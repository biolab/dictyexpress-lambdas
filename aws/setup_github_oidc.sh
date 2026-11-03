#!/usr/bin/env bash
# ONE-TIME: connect this GitHub repo to AWS for keyless deploys (OIDC).
# Creates (idempotently):
#   1. an IAM OIDC identity provider for GitHub Actions (if absent),
#   2. an IAM role the repo's `main` workflows can assume,
#   3. a least-privilege policy: update code+config on the lambda functions, and
#      stage code zips under deploy/ in the services' buckets (CODE_VIA_S3).
#
# Run once with admin-ish AWS credentials. Then set the printed ARN as the GitHub
# repository VARIABLE `AWS_DEPLOY_ROLE_ARN` (Settings > Secrets and variables >
# Actions > Variables). After that, pushes to main deploy automatically.
#
# Usage:  ./setup_github_oidc.sh [github-org/repo]   # default: biolab/dictyexpress-lambdas
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
source "${AWS_DIR}/common.env"

GH_REPO="${1:-biolab/dictyexpress-lambdas}"
OIDC_HOST="token.actions.githubusercontent.com"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_HOST}"
ROLE_NAME="${DEPLOY_ROLE_NAME:-github-actions-lambda-deploy}"

# Functions this role may update. Add new services here as you create them.
FUNCTIONS=("fast-find-similar" "dicty-go" "cell-de")
# Buckets of services with CODE_VIA_S3=true; the role may write their deploy/ prefix.
CODE_BUCKETS=("biolab-cell-de-us-east-1")

echo "Account ${ACCOUNT_ID} | repo ${GH_REPO} | role ${ROLE_NAME}"

# 1. OIDC provider -----------------------------------------------------------
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "${OIDC_ARN}" >/dev/null 2>&1; then
  echo "OIDC provider already exists."
else
  echo "Creating GitHub OIDC provider..."
  # AWS validates GitHub's certificate dynamically; the thumbprint is required by
  # the API but no longer security-relevant for this provider.
  aws iam create-open-id-connect-provider \
    --url "https://${OIDC_HOST}" \
    --client-id-list "sts.amazonaws.com" \
    --thumbprint-list "ffffffffffffffffffffffffffffffffffffffff" >/dev/null
fi

# 2. Role with a repo-scoped trust policy (only main branch can assume) -------
TRUST=$(cat <<JSON
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow",
  "Principal":{"Federated":"${OIDC_ARN}"},
  "Action":"sts:AssumeRoleWithWebIdentity",
  "Condition":{
    "StringEquals":{"${OIDC_HOST}:aud":"sts.amazonaws.com",
                    "${OIDC_HOST}:sub":"repo:${GH_REPO}:ref:refs/heads/main"}
  }
}]}
JSON
)
if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "Role exists; updating trust policy..."
  aws iam update-assume-role-policy --role-name "${ROLE_NAME}" \
    --policy-document "${TRUST}" >/dev/null
else
  echo "Creating role ${ROLE_NAME}..."
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "${TRUST}" >/dev/null
fi

# 3. Least-privilege deploy policy (update code+config on our functions only) -
RESOURCES=""
for fn in "${FUNCTIONS[@]}"; do
  RESOURCES+="\"arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${fn}\","
done
RESOURCES="[${RESOURCES%,}]"
CODE_OBJECTS=$(printf '"arn:aws:s3:::%s/deploy/*",' "${CODE_BUCKETS[@]}")
CODE_OBJECTS="[${CODE_OBJECTS%,}]"
# Lambda reads an S3 code zip with the caller's permissions, hence GetObject.
PERMS=$(cat <<JSON
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow",
  "Action":["lambda:GetFunction","lambda:GetFunctionConfiguration","lambda:UpdateFunctionCode","lambda:UpdateFunctionConfiguration"],
  "Resource":${RESOURCES}
},{
  "Effect":"Allow",
  "Action":["s3:PutObject","s3:GetObject","s3:AbortMultipartUpload"],
  "Resource":${CODE_OBJECTS}
}]}
JSON
)
aws iam put-role-policy --role-name "${ROLE_NAME}" \
  --policy-name lambda-deploy --policy-document "${PERMS}"

ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)
echo
echo "=================================================================="
echo "Done. Set this as a GitHub repository VARIABLE named AWS_DEPLOY_ROLE_ARN:"
echo "  ${ROLE_ARN}"
echo
echo "GitHub: Settings > Secrets and variables > Actions > Variables > New"
echo "Scope: only repo ${GH_REPO} @ main can assume it; can only update code on:"
printf '  - %s\n' "${FUNCTIONS[@]}"
echo "and stage code zips in:"
printf '  - s3://%s/deploy/\n' "${CODE_BUCKETS[@]}"
echo "=================================================================="
