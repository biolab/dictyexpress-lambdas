#!/usr/bin/env bash
# Remove a service's API Gateway + Lambda + IAM role. The S3 bucket (if any) is
# KEPT by default; pass --delete-bucket to also empty and remove it. Does NOT
# touch the shared app CloudFront distribution — remove the route's behavior +
# origin there manually if you want it gone.
#
# Usage:  ./teardown.sh <service> [--delete-bucket]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_service "${1:?usage: ./teardown.sh <service> [--delete-bucket]}"
DELETE_BUCKET=0
[ "${2:-}" = "--delete-bucket" ] && DELETE_BUCKET=1

echo "Tearing down ${SERVICE} (${FUNCTION_NAME}) in ${AWS_REGION}..."
echo "NOTE: does NOT remove the ${ROUTE} behavior from the shared app CloudFront."

API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}" \
  --query "Items[?Name=='${FUNCTION_NAME}'].ApiId | [0]" --output text 2>/dev/null)
if [ -n "${API_ID}" ] && [ "${API_ID}" != "None" ]; then
  aws apigatewayv2 delete-api --api-id "${API_ID}" --region "${AWS_REGION}" 2>/dev/null \
    && echo "deleted API Gateway ${API_ID}" || true
fi

aws lambda delete-function --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}" 2>/dev/null || true

aws iam delete-role-policy --role-name "${ROLE_NAME}" \
  --policy-name "${SERVICE}-s3" 2>/dev/null || true
aws iam detach-role-policy --role-name "${ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
aws iam delete-role --role-name "${ROLE_NAME}" 2>/dev/null || true

if [ "${NEEDS_S3}" = "true" ] && [ "${DELETE_BUCKET}" = "1" ]; then
  echo "Emptying and deleting bucket ${MATRIX_BUCKET}..."
  aws s3 rm "s3://${MATRIX_BUCKET}" --recursive 2>/dev/null || true
  aws s3api delete-bucket --bucket "${MATRIX_BUCKET}" --region "${AWS_REGION}" 2>/dev/null || true
elif [ "${NEEDS_S3}" = "true" ]; then
  echo "Kept bucket ${MATRIX_BUCKET} (pass --delete-bucket to remove it)."
fi

echo "Done."
