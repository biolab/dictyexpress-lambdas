#!/usr/bin/env bash
# Deploy a lambda service to AWS: (optional) S3 bucket + IAM role + Lambda +
# public HTTP API Gateway. Idempotent — safe to re-run to update code or config.
#
# Usage:  ./deploy.sh <service>     # e.g. ./deploy.sh find-similar | ./deploy.sh go
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_service "${1:?usage: ./deploy.sh <service>}"

echo "Account ${ACCOUNT_ID} | region ${AWS_REGION}"
echo "Service=${SERVICE} Function=${FUNCTION_NAME} Role=${ROLE_NAME} Route=${ROUTE}"
echo

# ---------------------------------------------------------------------------
# 1. Optional S3 bucket (private, versioned, AES256). Only if NEEDS_S3=true.
# ---------------------------------------------------------------------------
if [ "${NEEDS_S3}" = "true" ]; then
  : "${MATRIX_BUCKET:?NEEDS_S3=true requires MATRIX_BUCKET}"
  if aws s3api head-bucket --bucket "${MATRIX_BUCKET}" 2>/dev/null; then
    echo "Bucket already exists: ${MATRIX_BUCKET}"
  else
    echo "Creating bucket ${MATRIX_BUCKET}..."
    if [ "${AWS_REGION}" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "${MATRIX_BUCKET}" --region us-east-1
    else
      aws s3api create-bucket --bucket "${MATRIX_BUCKET}" --region "${AWS_REGION}" \
        --create-bucket-configuration LocationConstraint="${AWS_REGION}"
    fi
  fi
  aws s3api put-public-access-block --bucket "${MATRIX_BUCKET}" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-versioning --bucket "${MATRIX_BUCKET}" \
    --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "${MATRIX_BUCKET}" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
fi

# ---------------------------------------------------------------------------
# 2. IAM role: Lambda execution (+ read/write the bucket if NEEDS_S3).
# ---------------------------------------------------------------------------
TRUST_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "Role already exists: ${ROLE_NAME}"
else
  echo "Creating role ${ROLE_NAME}..."
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "${TRUST_POLICY}" >/dev/null
  aws iam attach-role-policy --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "Waiting for role to propagate..."; sleep 10
fi
if [ "${NEEDS_S3}" = "true" ]; then
  S3_POLICY="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::${MATRIX_BUCKET}\",\"arn:aws:s3:::${MATRIX_BUCKET}/*\"]}]}"
  aws iam put-role-policy --role-name "${ROLE_NAME}" \
    --policy-name "${SERVICE}-s3" --policy-document "${S3_POLICY}"
fi
ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)

# ---------------------------------------------------------------------------
# 3. Build the package
# ---------------------------------------------------------------------------
"${AWS_DIR}/build.sh" "${SERVICE}"

# Lambda env vars (space-separated KEY=VALUE pairs in LAMBDA_ENV -> --environment).
ENV_ARGS=()
if [ -n "${LAMBDA_ENV:-}" ]; then
  ENV_ARGS=(--environment "Variables={$(echo "${LAMBDA_ENV}" | tr ' ' ',')}")
fi

# ---------------------------------------------------------------------------
# 4. Create or update the Lambda function
# ---------------------------------------------------------------------------
if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "Updating function code + config..."
  aws lambda update-function-code --function-name "${FUNCTION_NAME}" \
    --zip-file "fileb://${ZIP_PATH}" --region "${AWS_REGION}" >/dev/null
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"
  aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" \
    --runtime "${LAMBDA_RUNTIME}" --handler "${HANDLER}" \
    --memory-size "${LAMBDA_MEMORY}" --timeout "${LAMBDA_TIMEOUT}" \
    "${ENV_ARGS[@]}" --region "${AWS_REGION}" >/dev/null
else
  echo "Creating function ${FUNCTION_NAME}..."
  aws lambda create-function --function-name "${FUNCTION_NAME}" \
    --runtime "${LAMBDA_RUNTIME}" --handler "${HANDLER}" \
    --role "${ROLE_ARN}" --zip-file "fileb://${ZIP_PATH}" \
    --memory-size "${LAMBDA_MEMORY}" --timeout "${LAMBDA_TIMEOUT}" \
    --architectures x86_64 \
    "${ENV_ARGS[@]}" --region "${AWS_REGION}" >/dev/null
fi
aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"

# ---------------------------------------------------------------------------
# 5. HTTP API Gateway in front of the Lambda (public HTTPS origin for CloudFront)
# ---------------------------------------------------------------------------
LAMBDA_ARN=$(aws lambda get-function --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}" --query 'Configuration.FunctionArn' --output text)
API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}" \
  --query "Items[?Name=='${FUNCTION_NAME}'].ApiId | [0]" --output text)
if [ "${API_ID}" = "None" ] || [ -z "${API_ID}" ]; then
  echo "Creating HTTP API Gateway ${FUNCTION_NAME}..."
  API_ID=$(aws apigatewayv2 create-api --name "${FUNCTION_NAME}" \
    --protocol-type HTTP --target "${LAMBDA_ARN}" --region "${AWS_REGION}" \
    --query 'ApiId' --output text)
else
  echo "API Gateway already exists: ${API_ID}"
fi
aws lambda add-permission --function-name "${FUNCTION_NAME}" \
  --statement-id apigw-invoke --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
  --region "${AWS_REGION}" >/dev/null 2>&1 || true
API_ENDPOINT=$(aws apigatewayv2 get-api --api-id "${API_ID}" --region "${AWS_REGION}" \
  --query 'ApiEndpoint' --output text)

# Throttle the public endpoint so abuse can't run up cost (default route settings
# on the auto-deployed $default stage).
if [ -n "${THROTTLE_RATE:-}" ] && [ -n "${THROTTLE_BURST:-}" ]; then
  echo "Setting throttle: ${THROTTLE_RATE} req/s, burst ${THROTTLE_BURST}"
  aws apigatewayv2 update-stage --api-id "${API_ID}" --stage-name '$default' \
    --default-route-settings "ThrottlingRateLimit=${THROTTLE_RATE},ThrottlingBurstLimit=${THROTTLE_BURST}" \
    --region "${AWS_REGION}" >/dev/null
fi

echo
echo "=================================================================="
echo "Deployed ${SERVICE}. API Gateway endpoint:"
echo "  ${API_ENDPOINT}"
echo
echo "Next:"
[ "${NEEDS_S3}" = "true" ] && echo "  ./upload_matrices.sh ${SERVICE}        # (optional) pre-seed S3"
echo "  ./cloudfront_attach.sh ${SERVICE} ${API_ID}   # add ${ROUTE} to the app CDN"
echo "=================================================================="
