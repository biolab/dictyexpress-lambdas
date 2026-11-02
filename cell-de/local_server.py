"""Local development server for selected-cells differential expression.

Run from the service directory after building an artifact with build_cellwise.py:

    python3 local_server.py

Exposes:

    POST http://localhost:8788/cell-de   {"strain": "AX4", "cells": [0, 5, ...]}
    (optional "reference": [...] cell indices; default all other cells)
"""

from __future__ import annotations

import os
import sys

# Make the repo-root `common` package importable when running this script
# directly from the service directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse  # noqa: E402
import json  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from time import perf_counter  # noqa: E402
from typing import Any  # noqa: E402

from common.cache import LruCache  # noqa: E402

from core import (  # noqa: E402
    STRAIN_PATTERN,
    Cellwise,
    compare_cells,
    load_cellwise,
    result_to_json,
)


class CellwiseStore:
    """Resolve cell-wise artifacts by strain: LRU memory -> disk."""

    def __init__(self, matrix_dir: Path, cache_bytes: int):
        self.matrix_dir = matrix_dir
        self.cache = LruCache(cache_bytes)

    def get(self, strain: str) -> Cellwise:
        cached = self.cache.get(strain)
        if cached is not None:
            return cached
        path = self.matrix_dir / f"{strain}.npz"
        if not path.exists():
            raise FileNotFoundError(f"No single-cell data for strain {strain}")
        times_path = self.matrix_dir / f"{strain}.times.npz"
        cellwise = load_cellwise(path, *([times_path] if times_path.exists() else []))
        self.cache.put(strain, cellwise)
        return cellwise


class CellDeHandler(BaseHTTPRequestHandler):
    """HTTP handler for local cell-de requests."""

    store: CellwiseStore

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST,OPTIONS")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/cell-de":
            self.write_json(404, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            started = perf_counter()
            strain = str(body["strain"])
            if not STRAIN_PATTERN.match(strain):
                raise ValueError(f"Invalid strain name: {strain!r}")
            reference = body.get("reference")
            result = compare_cells(
                self.store.get(strain),
                [int(c) for c in body["cells"]],
                None if reference is None else [int(c) for c in reference],
            )
            elapsed = perf_counter() - started
            print(f"OK strain={strain} n_selected={result['n_selected']} in {elapsed:.3f}s")
            payload = result_to_json(result)
            payload.update(strain=strain, timing={"seconds": elapsed})
            self.write_json(200, payload)
        except FileNotFoundError as error:
            self.write_json(404, {"error": str(error)})
        except (KeyError, ValueError, TypeError) as error:
            self.write_json(400, {"error": f"{type(error).__name__}: {error}"})

    def write_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix-dir", type=Path, default=Path(__file__).parent / "matrices")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--cache-mb", type=int, default=2048)
    args = parser.parse_args()

    CellDeHandler.store = CellwiseStore(args.matrix_dir, args.cache_mb * 1024 * 1024)
    server = ThreadingHTTPServer((args.host, args.port), CellDeHandler)
    print(f"Serving cell-de on http://{args.host}:{args.port}/cell-de ({args.matrix_dir})")
    server.serve_forever()


if __name__ == "__main__":
    main()
