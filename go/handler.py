"""AWS Lambda handler for dictyExpress GO enrichment.

Replaces the interactive `goenrichment` Resolwe process with a direct endpoint.
Runs the upstream gotea engine (compiled C++ `processor`, bundled in the zip) —
with one improvement: the p-value is the standard upper-tail hypergeometric
P(X >= matched) rather than gotea's single-point PMF (see gotea/processor.cpp).

Per (gaf source, species) we precompute the gotea `obo_obj` + `gaf_obj` binary
artifacts (build_objs.py) and store them in S3. On a request we:
  1. map the query gene ids to the GAF's database if they differ (KB mapping),
  2. download+cache the obj artifacts to /tmp,
  3. run the processor, and return its JSON (gene_ids in the GAF's database,
     exactly as the current dictyExpress site, which maps them back for display).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import traceback
from typing import Any

import boto3

from common.http import (
    http_method,
    parse_body,
    request_origin,
    requested_headers,
    response,
    response_raw,
)
from common.resolwe import ResolweError, map_gene_ids

S3 = boto3.client("s3")
HERE = os.path.dirname(os.path.abspath(__file__))
PROCESSOR = os.path.join(HERE, "processor")  # static x86_64 gotea binary in the zip
BUCKET = os.environ.get("GO_BUCKET", "")
PREFIX = os.environ.get("GO_PREFIX", "go").strip("/")
DEFAULT_PVAL = float(os.environ.get("GO_PVAL_THRESHOLD", "0.1"))
DEFAULT_MIN_GENES = int(os.environ.get("GO_MIN_GENES", "1"))

# Downloaded artifacts persist across warm invocations in /tmp.
_OBJ_DIR = "/tmp/go"


def _artifact(species: str, name: str) -> str:
    """Download obo_obj/gaf_obj/meta for a species to /tmp (cached), return path."""
    slug = species.replace(" ", "_")
    local = os.path.join(_OBJ_DIR, slug, name)
    if not os.path.exists(local):
        os.makedirs(os.path.dirname(local), exist_ok=True)
        S3.download_file(BUCKET, f"{PREFIX}/{slug}/{name}", local)
    return local


def _gaf_meta(species: str) -> dict:
    """Read the artifact's metadata: the GAF's source db + species."""
    with open(_artifact(species, "meta.json")) as handle:
        return json.load(handle)


def run_enrichment(
    genes: list[str], source: str, species: str, pval: float, min_genes: int
) -> bytes:
    """Map ids if needed, run the gotea processor, return its JSON output (bytes)."""
    meta = _gaf_meta(species)
    gaf_source = meta["gaf_source"]

    if source != gaf_source:
        mapping = map_gene_ids(source, gaf_source, species, genes)
        target_ids = [mapping[g] for g in genes if g in mapping]
        if not target_ids:
            raise ResolweError(
                f"Could not map any of {len(genes)} genes from {source} to {gaf_source}."
            )
    else:
        target_ids = genes

    obo_obj = _artifact(species, "obo_obj")
    gaf_obj = _artifact(species, "gaf_obj")

    with tempfile.NamedTemporaryFile("w", suffix=".txt") as genes_file:
        genes_file.write(" ".join(target_ids))
        genes_file.flush()
        # OMP_NUM_THREADS=1: the engine uses OpenMP, but Lambda has ~1-2 vCPUs and
        # the parallelism gives little benefit while risking thread-related crashes.
        result = subprocess.run(
            [PROCESSOR, str(pval), str(min_genes), obo_obj, gaf_obj, genes_file.name],
            capture_output=True,
            check=True,
            env={**os.environ, "OMP_NUM_THREADS": "1"},
        )
    # The processor already emits valid JSON; pass it through (no parse/re-serialize).
    return result.stdout


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle one GO enrichment request."""
    if event.get("warmup"):
        return {"warm": True}

    origin = request_origin(event)
    allow_headers = requested_headers(event)
    if http_method(event) == "OPTIONS":
        return response(200, {}, origin, allow_headers)

    try:
        body = parse_body(event)
        genes = [str(g) for g in body.get("genes", [])]
        if not genes:
            return response(400, {"error": "No genes provided."}, origin, allow_headers)
        source = str(body["source"])
        species = str(body["species"])
        pval = float(body.get("pval_threshold", DEFAULT_PVAL))
        min_genes = int(body.get("min_genes", DEFAULT_MIN_GENES))

        print(f"REQ go genes={len(genes)} source={source} species={species} pval={pval}")
        result = run_enrichment(genes, source, species, pval, min_genes)
        return response_raw(200, result.decode("utf-8"), origin, allow_headers)
    except ResolweError as error:
        print(f"FAIL 404 ResolweError: {error}")
        return response(404, {"error": str(error)}, origin, allow_headers)
    except subprocess.CalledProcessError as error:
        print(f"FAIL 500 processor: {error.stderr.decode('utf-8', 'replace')[:500]}")
        return response(500, {"error": "Enrichment engine failed."}, origin, allow_headers)
    except (KeyError, ValueError) as error:
        print(f"FAIL 400: {error}")
        return response(400, {"error": f"Bad request: {error}"}, origin, allow_headers)
    except Exception as error:  # noqa: BLE001
        print("FAIL 500 unexpected:\n" + traceback.format_exc())
        return response(500, {"error": f"{type(error).__name__}: {error}"}, origin, allow_headers)
