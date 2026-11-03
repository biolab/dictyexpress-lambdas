"""AWS Lambda handler: differential expression of selected cells vs a reference.

Request:  {"strain": "AX4", "cells": [...], "reference": [...]}  (reference optional;
          omitted = all other cells of the strain)
"""

from __future__ import annotations

import io
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common.cache import LruCache
from common.http import (
    http_method,
    parse_body,
    request_origin,
    requested_headers,
    response,
)

from core import STRAIN_PATTERN, Cellwise, compare_cells, load_cellwise, result_to_json


# A strain's artifact is ~0.5 GB in memory; the cap decides how many stay warm.
CACHE_MB = int(os.environ.get("MATRIX_CACHE_MB", "2048"))
MATRIX_CACHE = LruCache(CACHE_MB * 1024 * 1024)
FETCH_PART_BYTES = 16 * 1024 * 1024
FETCH_THREADS = 16
S3 = boto3.client("s3", config=Config(max_pool_connections=FETCH_THREADS))


class StrainNotFound(Exception):
    """No cell-wise artifact exists for the requested strain."""


def _matrix_key(strain: str, suffix: str = ".npz") -> str:
    prefix = os.environ.get("MATRIX_PREFIX", "cellwise").strip("/")
    return f"{prefix}/{strain}{suffix}"


def _is_missing(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404")


def _fetch(key: str) -> bytearray:
    """Download an S3 object in parallel ranged parts (one stream tops out ~90 MB/s)."""
    bucket = os.environ["MATRIX_BUCKET"]
    size = S3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    data = bytearray(size)

    def part(start: int) -> None:
        end = min(start + FETCH_PART_BYTES, size) - 1
        body = S3.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end}")["Body"]
        data[start : end + 1] = body.read()

    with ThreadPoolExecutor(FETCH_THREADS) as pool:
        list(pool.map(part, range(0, size, FETCH_PART_BYTES)))
    return data


def load_strain(strain: str) -> Cellwise:
    """Resolve a strain's cell-wise counts: warm-LRU -> S3."""
    cached = MATRIX_CACHE.get(strain)
    if cached is not None:
        return cached

    started = perf_counter()
    try:
        raw = _fetch(_matrix_key(strain))
    except ClientError as error:
        if _is_missing(error):
            raise StrainNotFound(strain) from error
        raise
    sources = [io.BytesIO(raw)]
    # Time point totals make "same time points" references as cheap as "all other cells".
    try:
        sources.append(io.BytesIO(_fetch(_matrix_key(strain, ".times.npz"))))
    except ClientError as error:
        if not _is_missing(error):
            raise
        print(f"no time point totals for {strain}")
    fetched = perf_counter()
    cellwise = load_cellwise(*sources)
    print(
        f"loaded {strain}: {len(raw) / 1e6:.0f} MB fetched in {fetched - started:.2f}s, "
        f"parsed in {perf_counter() - fetched:.2f}s"
    )
    MATRIX_CACHE.put(strain, cellwise)
    return cellwise


def _warm() -> None:
    """Keep the strains users land on loaded (best-effort).

    Runs on the keep-warm ping rather than at cold start: a strain's S3 fetch plus
    parse can pass Lambda's 10 s init limit, and a timed-out init is rerun inside
    the next request.
    """
    for strain in os.environ.get("WARM_STRAINS", "").replace(",", " ").split():
        try:
            load_strain(strain)
        except Exception as error:  # noqa: BLE001
            print(f"warm skipped for {strain}: {type(error).__name__}: {error}")


def _cell_indices(value: Any, field: str) -> list[int]:
    if not isinstance(value, list) or not all(
        isinstance(cell, int) and not isinstance(cell, bool) for cell in value
    ):
        raise ValueError(f"'{field}' must be a list of integer cell indices.")
    return value


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle one selected-cells differential expression request."""
    if event.get("warmup"):
        _warm()
        return {"warm": True}

    origin = request_origin(event)
    allow_headers = requested_headers(event)

    if http_method(event) == "OPTIONS":
        return response(200, {}, origin, allow_headers)

    try:
        body = parse_body(event)
        strain = str(body["strain"])
        if not STRAIN_PATTERN.match(strain):
            raise ValueError(f"Invalid strain name: {strain!r}")
        cells = _cell_indices(body["cells"], "cells")
        reference = body.get("reference")
        if reference is not None:
            reference = _cell_indices(reference, "reference")

        print(
            f"REQ strain={strain} n_cells={len(cells)} "
            f"n_reference={'rest' if reference is None else len(reference)}"
        )
        result = compare_cells(load_strain(strain), cells, reference)
        body = result_to_json(result)
        body["strain"] = strain
        return response(200, body, origin, allow_headers)
    except StrainNotFound as error:
        print(f"FAIL 404 StrainNotFound: {error}")
        return response(404, {"error": f"No single-cell data for strain {error}"}, origin, allow_headers)
    except KeyError as error:
        print(f"FAIL 400 KeyError: {error}")
        return response(400, {"error": f"Missing field: {error}"}, origin, allow_headers)
    except ValueError as error:
        print(f"FAIL 400 ValueError: {error}")
        return response(400, {"error": str(error)}, origin, allow_headers)
    except Exception as error:  # noqa: BLE001 - log unexpected, return 500
        print("FAIL 500 unexpected:\n" + traceback.format_exc())
        return response(500, {"error": f"{type(error).__name__}: {error}"}, origin, allow_headers)
