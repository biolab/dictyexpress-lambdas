"""AWS Lambda handler for fast dictyExpress find-similar queries."""

from __future__ import annotations

import io
import os
import traceback
from typing import Any

import boto3
import numpy as np
from botocore.exceptions import ClientError

from common.cache import LruCache
from common.http import (
    http_method,
    parse_body,
    request_origin,
    requested_headers,
    response,
)
from common.resolwe import ResolweError

from build_resolwe import build_matrix_from_resolwe
from core import compute_distances, load_npz_matrix


S3 = boto3.client("s3")
# Bounded LRU cache: a warm Lambda serving many time series keeps memory flat
# instead of growing with every distinct dataset it has ever seen.
CACHE_MB = int(os.environ.get("MATRIX_CACHE_MB", "256"))
RESULT_LIMIT = int(os.environ.get("FIND_SIMILAR_TOP_N", "0")) or None
MATRIX_CACHE = LruCache(CACHE_MB * 1024 * 1024)


def _matrix_key(time_series_id: int) -> str:
    prefix = os.environ.get("MATRIX_PREFIX", "matrices").strip("/")
    return f"{prefix}/{time_series_id}.npz"


def _persist_matrix(bucket: str, key: str, matrix: dict[str, np.ndarray]) -> None:
    """Write a freshly built matrix to S3 so later requests skip the build."""
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **matrix)
    S3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())


def load_matrix(time_series_id: int, expression_ids: list[int]) -> dict[str, np.ndarray]:
    """Resolve a matrix: warm-LRU -> S3 -> lazy build from Resolwe (then persist)."""
    cached = MATRIX_CACHE.get(time_series_id)
    if cached is not None:
        return cached

    bucket = os.environ["MATRIX_BUCKET"]
    key = _matrix_key(time_series_id)

    try:
        obj = S3.get_object(Bucket=bucket, Key=key)
        matrix = load_npz_matrix(io.BytesIO(obj["Body"].read()))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
            raise
        # Not in S3 yet: build it from Resolwe on demand, then cache to S3 so
        # this dataset (and any new dataset) is fast on every later request.
        matrix = build_matrix_from_resolwe(time_series_id, expression_ids)
        _persist_matrix(bucket, key, matrix)

    MATRIX_CACHE.put(time_series_id, matrix)
    return matrix


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle one fast find-similar request."""
    origin = request_origin(event)
    allow_headers = requested_headers(event)

    # CORS preflight: a cross-origin browser POST with JSON triggers an OPTIONS
    # request first. Answer it with the CORS headers (see common.http.response).
    if http_method(event) == "OPTIONS":
        return response(200, {}, origin, allow_headers)

    try:
        body = parse_body(event)
        time_series_id = int(body["time_series"])
        gene = str(body["gene"])
        distance = str(body.get("distance", "spearman"))

        expression_ids = [int(i) for i in body.get("expressions", [])]
        limit = body.get("limit", RESULT_LIMIT)
        limit = int(limit) if limit is not None else None

        print(
            f"REQ time_series={time_series_id} gene={gene} distance={distance} "
            f"n_expr={len(expression_ids)}"
        )
        matrix = load_matrix(time_series_id, expression_ids)
        similar_genes = compute_distances(matrix, gene, distance, limit=limit)

        return response(
            200,
            {"search gene": gene, "similar genes": similar_genes},
            origin,
            allow_headers,
        )
    except ResolweError as error:  # includes LazyBuildUnavailable
        print(f"FAIL 404 ResolweError: {error}")
        return response(404, {"error": str(error)}, origin, allow_headers)
    except KeyError as error:
        print(f"FAIL 404 KeyError: {error}")
        return response(404, {"error": f"Gene or field not found: {error}"}, origin, allow_headers)
    except ValueError as error:
        print(f"FAIL 400 ValueError: {error}")
        return response(400, {"error": str(error)}, origin, allow_headers)
    except Exception as error:  # noqa: BLE001 - log unexpected, return 500
        print("FAIL 500 unexpected:\n" + traceback.format_exc())
        return response(500, {"error": f"{type(error).__name__}: {error}"}, origin, allow_headers)
