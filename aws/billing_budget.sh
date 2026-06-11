#!/usr/bin/env bash
# Account-wide AWS cost budget with email alerts, so a runaway never surprises
# you. Idempotent. Note: AWS Budgets are account-wide (not per-service); current
# account run rate is well under the limit, so this guards the whole account.
#
# Usage:  ALERT_EMAIL=you@example.com ./billing_budget.sh [limit_usd]
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
source "${AWS_DIR}/common.env"

LIMIT="${1:-2}"
ALERT_EMAIL="${ALERT_EMAIL:-lena.trnovec@gmail.com}"
NAME="dictyexpress-monthly-${LIMIT}usd"

echo "Budget '${NAME}': \$${LIMIT}/month, alerts -> ${ALERT_EMAIL} (account ${ACCOUNT_ID})"

BUDGET=$(cat <<JSON
{"BudgetName":"${NAME}","BudgetLimit":{"Amount":"${LIMIT}","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}
JSON
)

# Alert at 80% actual, 100% actual, and 100% forecasted.
NOTIFS=$(cat <<JSON
[
 {"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80,"ThresholdType":"PERCENTAGE"},
  "Subscribers":[{"SubscriptionType":"EMAIL","Address":"${ALERT_EMAIL}"}]},
 {"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":100,"ThresholdType":"PERCENTAGE"},
  "Subscribers":[{"SubscriptionType":"EMAIL","Address":"${ALERT_EMAIL}"}]},
 {"Notification":{"NotificationType":"FORECASTED","ComparisonOperator":"GREATER_THAN","Threshold":100,"ThresholdType":"PERCENTAGE"},
  "Subscribers":[{"SubscriptionType":"EMAIL","Address":"${ALERT_EMAIL}"}]}
]
JSON
)

if aws budgets describe-budget --account-id "${ACCOUNT_ID}" --budget-name "${NAME}" >/dev/null 2>&1; then
  echo "Budget exists; updating limit..."
  aws budgets update-budget --account-id "${ACCOUNT_ID}" --new-budget "${BUDGET}" >/dev/null
else
  aws budgets create-budget --account-id "${ACCOUNT_ID}" \
    --budget "${BUDGET}" --notifications-with-subscribers "${NOTIFS}" >/dev/null
fi
echo "Done. Alerts at 80% (\$$(echo "${LIMIT} * 0.8" | bc)) and 100% (\$${LIMIT}), plus forecasted-over."
