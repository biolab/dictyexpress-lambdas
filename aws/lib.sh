# Shared helpers for the deploy scripts. Sourced by build.sh / deploy.sh /
# teardown.sh / cloudfront_attach.sh / upload_matrices.sh.
#
# load_service <name> resolves paths and sources common.env + <service>/service.env,
# exporting: SERVICE, SERVICE_DIR, REPO_ROOT, BUILD_DIR, DIST_DIR, ZIP_PATH and
# all the service config (FUNCTION_NAME, ROLE_NAME, ROUTE, NEEDS_S3, ...).

AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${AWS_DIR}")"

load_service() {
  SERVICE="${1:?usage: <service>  (e.g. find-similar | go)}"
  SERVICE_DIR="${REPO_ROOT}/${SERVICE}"
  if [ ! -f "${SERVICE_DIR}/service.env" ]; then
    echo "Unknown service '${SERVICE}' (no ${SERVICE_DIR}/service.env)" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "${AWS_DIR}/common.env"
  # shellcheck disable=SC1090
  source "${SERVICE_DIR}/service.env"

  DIST_DIR="${AWS_DIR}/dist"
  BUILD_DIR="${AWS_DIR}/build/${SERVICE}"
  ZIP_PATH="${DIST_DIR}/${SERVICE}.zip"
}

# Sets CREATE_CODE_ARGS / UPDATE_CODE_ARGS for the Lambda package. Direct uploads
# are capped at 50 MB and time out near that size on slow connections, so a
# service with CODE_VIA_S3=true stages the zip in its BUCKET first.
stage_code() {
  if [ "${CODE_VIA_S3:-false}" = "true" ]; then
    local key="deploy/${SERVICE}.zip"
    echo "Staging ${ZIP_PATH} at s3://${BUCKET}/${key}..."
    aws s3 cp "${ZIP_PATH}" "s3://${BUCKET}/${key}" --only-show-errors
    CREATE_CODE_ARGS=(--code "S3Bucket=${BUCKET},S3Key=${key}")
    UPDATE_CODE_ARGS=(--s3-bucket "${BUCKET}" --s3-key "${key}")
  else
    CREATE_CODE_ARGS=(--zip-file "fileb://${ZIP_PATH}")
    UPDATE_CODE_ARGS=(--zip-file "fileb://${ZIP_PATH}")
  fi
}
