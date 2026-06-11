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
