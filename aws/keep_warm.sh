#!/usr/bin/env bash
# Keep one Lambda container (and its in-memory cache) warm with a scheduled
# EventBridge ping every 5 minutes. The handler short-circuits {"warmup": true}
# events. Cheap alternative to provisioned concurrency for low-concurrency use.
# Idempotent. Pass --remove to delete the schedule.
#
# Usage:  ./keep_warm.sh <service> [--remove]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_service "${1:?usage: ./keep_warm.sh <service> [--remove]}"

RULE="${FUNCTION_NAME}-keepwarm"
RULE_ARN="arn:aws:events:${AWS_REGION}:${ACCOUNT_ID}:rule/${RULE}"
LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

if [ "${2:-}" = "--remove" ]; then
  aws events remove-targets --rule "${RULE}" --ids 1 --region "${AWS_REGION}" 2>/dev/null || true
  aws events delete-rule --name "${RULE}" --region "${AWS_REGION}" 2>/dev/null || true
  aws lambda remove-permission --function-name "${FUNCTION_NAME}" \
    --statement-id keepwarm --region "${AWS_REGION}" 2>/dev/null || true
  echo "Removed keep-warm schedule for ${FUNCTION_NAME}."
  exit 0
fi

echo "Creating keep-warm schedule (rate 5 min) for ${FUNCTION_NAME}..."
aws events put-rule --name "${RULE}" --schedule-expression "rate(5 minutes)" \
  --region "${AWS_REGION}" >/dev/null

aws lambda add-permission --function-name "${FUNCTION_NAME}" \
  --statement-id keepwarm --action lambda:InvokeFunction \
  --principal events.amazonaws.com --source-arn "${RULE_ARN}" \
  --region "${AWS_REGION}" >/dev/null 2>&1 || true

aws events put-targets --rule "${RULE}" --region "${AWS_REGION}" \
  --targets "Id=1,Arn=${LAMBDA_ARN},Input='{\"warmup\":true}'" >/dev/null

echo "Done. ${FUNCTION_NAME} is pinged every 5 minutes."
