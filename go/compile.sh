#!/usr/bin/env bash
# Compile the (improved) gotea processor + preprocessor as STATIC x86_64 binaries
# for AWS Lambda. Uses native g++ on linux/x86_64 (e.g. GitHub Actions), otherwise
# builds in a docker gcc image (e.g. on a Mac).
#
# Usage:  ./compile.sh [out_dir]   # default out_dir: bin
set -euo pipefail
cd "$(dirname "$0")"
OUT="${1:-bin}"
mkdir -p "$OUT"

CXXFLAGS="-Wall -std=c++11 -O3 -static -lm"
PROC_SRC="gotea/processor.cpp"
PRE_SRC="gotea/preprocessor.cpp gotea/oboparser.cpp gotea/gafparser.cpp gotea/termbuilder.cpp gotea/linkbuilder.cpp"

if [ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] && command -v g++ >/dev/null 2>&1; then
  echo "Compiling natively (linux/x86_64)..."
  g++ $CXXFLAGS $PROC_SRC -fopenmp -fwhole-program -o "$OUT/processor"
  g++ $CXXFLAGS $PRE_SRC -o "$OUT/preprocessor"
else
  echo "Compiling via docker (x86_64)..."
  docker run --rm --platform linux/amd64 -v "$PWD":/w -w /w gcc:12 bash -c "
    g++ $CXXFLAGS $PROC_SRC -fopenmp -fwhole-program -o $OUT/processor &&
    g++ $CXXFLAGS $PRE_SRC -o $OUT/preprocessor"
fi
echo "Built $OUT/processor and $OUT/preprocessor"
