#!/usr/bin/env python3
"""Pre-build a matrix for every bcm time series and upload to S3.

Pre-seeding S3 means users never trigger a (multi-second) on-demand build — a
cache miss becomes a fast S3 fetch. Matrices are built with the exact same code
path as the lazy build, so artifacts are identical.

Run from the find-similar directory:

    python3 prebuild_all.py                 # build missing, upload to S3
    python3 prebuild_all.py --force         # rebuild all (ignore what's in S3)
    python3 prebuild_all.py --no-upload     # build locally only
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

# Make the repo-root `common` package importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from common.resolwe import (  # noqa: E402
    expression_data_ids_for_relation,
    list_time_series,
)

from build_resolwe import build_matrix_from_resolwe  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MATRIX_DIR = os.path.join(HERE, "matrices")
BUCKET = os.environ.get("MATRIX_BUCKET", "biolab-fast-find-similar-us-east-1")
PREFIX = os.environ.get("MATRIX_PREFIX", "matrices")


def existing_s3_ids() -> set[int]:
    """Time-series ids already present as matrices/<id>.npz in S3."""
    try:
        out = subprocess.check_output(
            ["aws", "s3", "ls", f"s3://{BUCKET}/{PREFIX}/"], text=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
    ids = set()
    for line in out.splitlines():
        name = line.split()[-1]
        if name.endswith(".npz"):
            try:
                ids.add(int(name[:-4]))
            except ValueError:
                pass
    return ids


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--community", default="bcm")
    parser.add_argument("--force", action="store_true", help="rebuild ids already in S3")
    parser.add_argument("--no-upload", action="store_true", help="build locally only")
    args = parser.parse_args()

    os.makedirs(MATRIX_DIR, exist_ok=True)
    relations = sorted(list_time_series(args.community), key=lambda r: r["id"])
    in_s3 = set() if args.force else existing_s3_ids()
    print(f"{len(relations)} time series in community:{args.community}; {len(in_s3)} already in S3")

    built, skipped, failed = [], [], []
    for relation in relations:
        rid = int(relation["id"])
        if rid in in_s3:
            skipped.append(rid)
            continue

        started = time.perf_counter()
        try:
            expr_ids = expression_data_ids_for_relation(relation)
            matrix = build_matrix_from_resolwe(rid, expr_ids)
            np.savez_compressed(os.path.join(MATRIX_DIR, f"{rid}.npz"), **matrix)
            g, t = matrix["expression"].shape
            print(f"[ok]   {rid}: {g} genes x {t} tp, {len(expr_ids)} samples, "
                  f"{time.perf_counter() - started:.1f}s")
            built.append(rid)
        except Exception as error:  # noqa: BLE001 - report and continue
            print(f"[FAIL] {rid}: {type(error).__name__}: {error}")
            failed.append((rid, str(error)))

    print(f"\nBuilt {len(built)} | skipped {len(skipped)} (already in S3) | failed {len(failed)}")
    for rid, error in failed:
        print(f"  FAIL {rid}: {error}")

    if not args.no_upload and built:
        print("\nSyncing matrices to S3...")
        subprocess.run([os.path.join(HERE, "..", "aws", "upload_matrices.sh")], check=False)


if __name__ == "__main__":
    main()
