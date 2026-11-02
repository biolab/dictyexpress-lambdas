#!/usr/bin/env bash
# Build a lambda deployment zip: <service>'s source + the shared common/ package
# + third-party deps (Linux wheels). boto3/botocore are skipped — they ship in
# the Lambda runtime. No Docker needed.
#
# Usage:  ./build.sh <service>      # e.g. ./build.sh find-similar
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_service "${1:?usage: ./build.sh <service>}"

echo "Building ${SERVICE} for ${LAMBDA_RUNTIME} (linux x86_64)..."
rm -rf "${BUILD_DIR}" "${ZIP_PATH}"
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

# Third-party deps from requirements.txt, minus boto3/botocore (in the runtime).
REQ="${BUILD_DIR}/.requirements.txt"
grep -ivE '^(boto3|botocore)([=<>! ]|$)' "${SERVICE_DIR}/requirements.txt" > "${REQ}" || true
if [ -s "${REQ}" ]; then
  pip3 install \
    --platform manylinux2014_x86_64 --implementation cp \
    --python-version "${PYTHON_VERSION}" --only-binary=:all: \
    --target "${BUILD_DIR}" -r "${REQ}"
fi
rm -f "${REQ}"
# scipy's test suites never run in Lambda and would push the cell-de zip past
# the 50 MB direct-upload limit. (Not numpy's: numpy.testing imports from them.)
if [ -d "${BUILD_DIR}/scipy" ]; then
  find "${BUILD_DIR}/scipy" -type d -name tests -prune -exec rm -rf {} +
fi

# Shared package + service source (exclude dev-only local_server.py and tests).
cp -R "${REPO_ROOT}/common" "${BUILD_DIR}/common"
for f in "${SERVICE_DIR}"/*.py; do
  case "$(basename "$f")" in local_server.py | test_*.py) continue ;; esac
  cp "$f" "${BUILD_DIR}/"
done

# Native runtime binary (e.g. gotea `processor` for GO): compile + bundle it.
# compile.sh writes to its own bin/ (relative, so it works inside the build
# container too); we copy only the runtime binary into the zip.
if [ -x "${SERVICE_DIR}/compile.sh" ]; then
  echo "Compiling native binaries for ${SERVICE}..."
  ( cd "${SERVICE_DIR}" && ./compile.sh )
  [ -f "${SERVICE_DIR}/bin/processor" ] && cp "${SERVICE_DIR}/bin/processor" "${BUILD_DIR}/"
fi

( cd "${BUILD_DIR}" && zip -qr "${ZIP_PATH}" . -x '*/__pycache__/*' '*.pyc' )
echo "Built ${ZIP_PATH} ($(du -h "${ZIP_PATH}" | cut -f1))"
