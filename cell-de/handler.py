"""AWS Lambda handler: differential expression of selected cells vs a reference.

Request:  {"strain": "AX4", "cells": [...], "reference": [...]}  (reference optional;
          omitted = all other cells of the strain)
"""

from __future__ import annotations

import io
import os
import traceback
from typing import Any

import boto3
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


S3 = boto3.client("s3")
# A strain's artifact is ~0.5 GB in memory; the cap decides how many stay warm.
CACHE_MB = int(os.environ.get("MATRIX_CACHE_MB", "2048"))
MATRIX_CACHE = LruCache(CACHE_MB * 1024 * 1024)


class StrainNotFound(Exception):
    """No cell-wise artifact exists for the requested strain."""


def _matrix_key(strain: str) -> str:
    prefix = os.environ.get("MATRIX_PREFIX", "cellwise").strip("/")
    return f"{prefix}/{strain}.npz"


def load_strain(strain: str) -> Cellwise:
    """Resolve a strain's cell-wise counts: warm-LRU -> S3."""
    cached = MATRIX_CACHE.get(strain)
    if cached is not None:
        return cached

    try:
        obj = S3.get_object(Bucket=os.environ["MATRIX_BUCKET"], Key=_matrix_key(strain))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise StrainNotFound(strain) from error
        raise
    cellwise = load_cellwise(io.BytesIO(obj["Body"].read()))
    MATRIX_CACHE.put(strain, cellwise)
    return cellwise


def _preload() -> None:
    """Load the default strain at cold start (best-effort, like find-similar)."""
    for strain in os.environ.get("PRELOAD_STRAINS", "").replace(",", " ").split():
        try:
            load_strain(strain)
            print(f"preloaded strain {strain}")
        except Exception as error:  # noqa: BLE001
            print(f"preload skipped for {strain}: {type(error).__name__}: {error}")


_preload()


def _cell_indices(value: Any, field: str) -> list[int]:
    if not isinstance(value, list) or not all(
        isinstance(cell, int) and not isinstance(cell, bool) for cell in value
    ):
        raise ValueError(f"'{field}' must be a list of integer cell indices.")
    return value


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle one selected-cells differential expression request."""
    if event.get("warmup"):
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
