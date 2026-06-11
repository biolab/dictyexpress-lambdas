"""Local development server for fast find-similar.

Run from the service directory:

    python3 local_server.py

Exposes:

    POST http://localhost:8787/find-similar

Memory is bounded by a byte-capped LRU cache (--cache-mb). Matrices missing from
disk are built on demand from Resolwe, persisted as a small .npz, and served from
cache thereafter.
"""

from __future__ import annotations

import os
import sys

# Make the repo-root `common` package importable when running this script
# directly from the service directory. In Lambda, `common/` is bundled at the
# package root, so this insert is a harmless no-op there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse  # noqa: E402
import json  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from time import perf_counter  # noqa: E402
from typing import Any, Optional  # noqa: E402

import numpy as np  # noqa: E402

from common.cache import LruCache  # noqa: E402
from common.resolwe import ResolweError  # noqa: E402

from build_resolwe import build_matrix_from_resolwe  # noqa: E402
from core import Matrix, compute_distances, load_npz_matrix  # noqa: E402


class MatrixStore:
    """Resolve matrices by time-series id: LRU memory -> disk -> lazy build."""

    def __init__(self, matrix_dir: Path, cache_bytes: int):
        self.matrix_dir = matrix_dir
        self.cache = LruCache(cache_bytes)

    def get(self, time_series_id: int, expression_ids: list[int]) -> Matrix:
        """Return a matrix, loading or building it if not already cached."""
        cached = self.cache.get(time_series_id)
        if cached is not None:
            return cached

        path = self.matrix_dir / f"{time_series_id}.npz"
        if path.exists():
            matrix = load_npz_matrix(path)
        else:
            matrix = self._build_and_persist(time_series_id, expression_ids, path)

        self.cache.put(time_series_id, matrix)
        return matrix

    def _build_and_persist(
        self, time_series_id: int, expression_ids: list[int], path: Path
    ) -> Matrix:
        """Build a matrix on demand from Resolwe and cache it to disk."""
        matrix = build_matrix_from_resolwe(time_series_id, expression_ids)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, **matrix)
        print(f"Built and cached matrix for time series {time_series_id} -> {path}")
        return matrix


class FindSimilarHandler(BaseHTTPRequestHandler):
    """HTTP handler for local find-similar requests."""

    matrix_store: MatrixStore
    result_limit: Optional[int] = None

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST,OPTIONS")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/find-similar":
            self.write_json(404, {"error": "Not found"})
            return

        body: dict[str, Any] = {}
        try:
            body = self.read_json_body()
            started = perf_counter()
            time_series_id = int(body["time_series"])
            gene = str(body["gene"])
            distance = str(body.get("distance", "spearman"))
            expression_ids = [int(i) for i in body.get("expressions", [])]
            limit = body.get("limit", self.result_limit)
            limit = int(limit) if limit is not None else None

            matrix = self.matrix_store.get(time_series_id, expression_ids)
            similar_genes = compute_distances(matrix, gene, distance, limit=limit)
            elapsed = perf_counter() - started

            print(
                f"OK time_series={time_series_id} gene={gene} distance={distance} "
                f"-> {len(similar_genes)} genes in {elapsed:.3f}s"
            )
            self.write_json(
                200,
                {
                    "search gene": gene,
                    "similar genes": similar_genes,
                    "timing": {"seconds": elapsed},
                },
            )
        except ResolweError as error:
            self.log_failure(body, error)
            self.write_json(404, {"error": str(error)})
        except KeyError as error:
            self.log_failure(body, f"Gene or field not found: {error}")
            self.write_json(404, {"error": f"Gene or field not found: {error}"})
        except ValueError as error:
            self.log_failure(body, error)
            self.write_json(400, {"error": str(error)})
        except json.JSONDecodeError:
            self.log_failure(body, "Invalid JSON body")
            self.write_json(400, {"error": "Invalid JSON body"})

    def log_failure(self, body: dict[str, Any], reason: Any) -> None:
        """Print why a request failed, with the requested dataset/gene."""
        print(
            f"FAIL time_series={body.get('time_series')} gene={body.get('gene')} "
            f"distance={body.get('distance')} -> {reason}"
        )

    def read_json_body(self) -> dict[str, Any]:
        """Read the request body as JSON."""
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def write_json(self, status: int, body: dict[str, Any]) -> None:
        """Write a JSON response."""
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep local logs concise."""
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--matrix-dir",
        type=Path,
        default=Path(__file__).parent / "matrices",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--cache-mb",
        type=int,
        default=64,
        help="Max MB of matrices held in memory (LRU eviction past this).",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=0,
        help="Return only the N closest genes (0 = all). Shrinks the response.",
    )
    args = parser.parse_args()

    FindSimilarHandler.matrix_store = MatrixStore(
        args.matrix_dir, cache_bytes=args.cache_mb * 1024 * 1024
    )
    FindSimilarHandler.result_limit = args.top_n if args.top_n > 0 else None

    server = ThreadingHTTPServer((args.host, args.port), FindSimilarHandler)
    print(
        f"Serving fast find-similar on http://{args.host}:{args.port}/find-similar\n"
        f"  matrices: {args.matrix_dir}\n"
        f"  memory cap: {args.cache_mb} MB (LRU)\n"
        f"  result limit: {FindSimilarHandler.result_limit or 'all genes'}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
