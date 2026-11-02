#!/usr/bin/env bash
# Update ONLY a lambda's code + config (no infra). For CI / routine deploys once
# deploy.sh has provisioned the bucket / IAM role / API Gateway. Needs just
# lambda:GetFunction + lambda:UpdateFunctionCode + lambda:UpdateFunctionConfiguration.
#
# Usage:  ./update_code.sh <service>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_service "${1:?usage: ./update_code.sh <service>}"

if ! aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "Function ${FUNCTION_NAME} does not exist yet — run ./deploy.sh ${SERVICE} first (provisions infra)." >&2
  exit 1
fi

"${AWS_DIR}/build.sh" "${SERVICE}"
stage_code

ENV_ARGS=()
if [ -n "${LAMBDA_ENV:-}" ]; then
  ENV_ARGS=(--environment "Variables={$(echo "${LAMBDA_ENV}" | tr ' ' ',')}")
fi

echo "Updating code for ${FUNCTION_NAME}..."
aws lambda update-function-code --function-name "${FUNCTION_NAME}" \
  "${UPDATE_CODE_ARGS[@]}" --region "${AWS_REGION}" >/dev/null
aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"

aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" \
  --runtime "${LAMBDA_RUNTIME}" --handler "${HANDLER}" \
  --memory-size "${LAMBDA_MEMORY}" --timeout "${LAMBDA_TIMEOUT}" \
  "${ENV_ARGS[@]}" --region "${AWS_REGION}" >/dev/null
aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"

echo "Updated ${FUNCTION_NAME}."
