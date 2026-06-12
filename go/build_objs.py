#!/usr/bin/env python3
"""Build gotea obo_obj + gaf_obj artifacts for a species and upload to S3.

Offline step (analogous to find-similar's prebuild). For each GO dataset it:
  1. downloads the OBO + GAF files from the Resolwe backend (public signed-URL
     redirect at app.genialis.com/data/<id>/<file>),
  2. runs the gotea `preprocessor` (built by compile.sh) to make obo_obj/gaf_obj,
  3. writes meta.json (the GAF's source db + species, for id mapping),
  4. uploads obo_obj, gaf_obj, meta.json to s3://<bucket>/<prefix>/<species>/.

Datasets are defined in DATASETS below (the dicty one matches the live config:
OBO data 24330, GAF data 490688 source=UniProtKB).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RESOLWE = os.environ.get("RESOLWE_FILE_HOST", "https://app.genialis.com")

# species -> {obo (data id, file), gaf (data id, file), gaf_source}
DATASETS = {
    "Dictyostelium discoideum": {
        "obo": (24330, "go.obo.gz"),
        "gaf": (490688, "dicty_go_associations_amigo_2020_10_06.gaf.txt.gz"),
        "gaf_source": "UniProtKB",
    },
}


def _download_gunzip(data_id: int, filename: str, dest: str) -> None:
    url = f"{RESOLWE}/data/{data_id}/{filename}"
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest + ".gz", "wb") as out:
        shutil.copyfileobj(resp, out)
    with gzip.open(dest + ".gz", "rb") as gz, open(dest, "wb") as out:
        shutil.copyfileobj(gz, out)
    os.remove(dest + ".gz")


def build_species(species: str, bucket: str, prefix: str, preprocessor: str) -> None:
    cfg = DATASETS[species]
    slug = species.replace(" ", "_")
    with tempfile.TemporaryDirectory() as tmp:
        obo_txt = os.path.join(tmp, "go.obo")
        gaf_txt = os.path.join(tmp, "annotations.gaf")
        _download_gunzip(*cfg["obo"], dest=obo_txt)
        _download_gunzip(*cfg["gaf"], dest=gaf_txt)

        obo_obj = os.path.join(tmp, "obo_obj")
        gaf_obj = os.path.join(tmp, "gaf_obj")
        subprocess.run([preprocessor, "obo", obo_txt, obo_obj], check=True)
        subprocess.run([preprocessor, "gaf", gaf_txt, gaf_obj], check=True)

        meta = os.path.join(tmp, "meta.json")
        with open(meta, "w") as handle:
            json.dump({"gaf_source": cfg["gaf_source"], "species": species}, handle)

        import boto3

        s3 = boto3.client("s3")
        for name, path in [("obo_obj", obo_obj), ("gaf_obj", gaf_obj), ("meta.json", meta)]:
            key = f"{prefix}/{slug}/{name}"
            print(f"  uploading s3://{bucket}/{key} ({os.path.getsize(path)} bytes)")
            s3.upload_file(path, bucket, key)
    print(f"[ok] {species}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", default=os.environ.get("GO_BUCKET"))
    parser.add_argument("--prefix", default=os.environ.get("GO_PREFIX", "go"))
    parser.add_argument("--preprocessor", default=os.path.join(HERE, "bin", "preprocessor"))
    parser.add_argument("--species", action="append", help="default: all in DATASETS")
    args = parser.parse_args()

    if not args.bucket:
        parser.error("--bucket (or GO_BUCKET) is required")
    if not os.path.exists(args.preprocessor):
        parser.error(f"preprocessor not found at {args.preprocessor}; run ./compile.sh first")

    for species in (args.species or list(DATASETS)):
        print(f"building {species}...")
        build_species(species, args.bucket, args.prefix, args.preprocessor)


if __name__ == "__main__":
    main()
