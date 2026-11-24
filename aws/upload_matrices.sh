#!/usr/bin/env bash
# Upload a service's precomputed matrices to its S3 bucket. Re-run any time you
# build new ones; `aws s3 sync` only transfers changed/added files.
#
# Usage:  ./upload_matrices.sh [service]   # defaults to find-similar
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_service "${1:-find-similar}"

if [ "${NEEDS_S3}" != "true" ]; then
  echo "${SERVICE} has NEEDS_S3=false — nothing to upload." ; exit 1
fi

echo "Syncing ${SERVICE_DIR}/matrices/*.npz -> s3://${MATRIX_BUCKET}/${MATRIX_PREFIX}/"
aws s3 sync "${SERVICE_DIR}/matrices/" "s3://${MATRIX_BUCKET}/${MATRIX_PREFIX}/" \
  --exclude "*" --include "*.npz"

echo
echo "Objects in s3://${MATRIX_BUCKET}/${MATRIX_PREFIX}/:"
aws s3 ls "s3://${MATRIX_BUCKET}/${MATRIX_PREFIX}/"

# find-similar also serves per-sample expressions (time courses) from samples/.
if [ -d "${SERVICE_DIR}/samples" ]; then
  SAMPLES_PREFIX="${SAMPLES_PREFIX:-samples}"
  echo
  echo "Syncing ${SERVICE_DIR}/samples/*.npz -> s3://${MATRIX_BUCKET}/${SAMPLES_PREFIX}/"
  aws s3 sync "${SERVICE_DIR}/samples/" "s3://${MATRIX_BUCKET}/${SAMPLES_PREFIX}/" \
    --exclude "*" --include "*.npz"
fi
