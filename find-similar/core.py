"""Matrix loading and distance calculations for fast find-similar.

Matrices are stored on disk/S3 as `.npz` artifacts (genes x timepoints,
replicate-averaged, constant genes removed). The in-memory LRU cache lives in
common.cache; this module is just the find-similar specific math.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

Matrix = dict[str, np.ndarray]


def load_npz_matrix(path: Path | str) -> Matrix:
    """Load a precomputed time-series matrix from a NumPy .npz file."""
    loaded = np.load(path, allow_pickle=False)
    return {
        "genes": loaded["genes"],
        "expression": np.asarray(loaded["expression"], dtype=np.float64),
        "rank_expression": np.asarray(loaded["rank_expression"], dtype=np.float64),
        "timepoints": loaded["timepoints"],
        **(
            {"expression_ids": np.asarray(loaded["expression_ids"], dtype=np.int64)}
            if "expression_ids" in loaded
            else {}
        ),
    }


def correlation_distance(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Return 1 - correlation(query, each row in matrix)."""
    centered = matrix - matrix.mean(axis=1, keepdims=True)
    query_centered = query - query.mean()
    denominator = np.linalg.norm(centered, axis=1) * np.linalg.norm(query_centered)

    distances = np.full(matrix.shape[0], np.inf, dtype=np.float64)
    valid = denominator > 0
    distances[valid] = 1.0 - (
        centered[valid] @ query_centered / denominator[valid]
    )
    return distances


def compute_distances(
    matrix: Matrix,
    gene: str,
    distance: str,
    limit: Optional[int] = None,
) -> list[dict[str, float | str]]:
    """Compute and sort distances from the query gene to all genes.

    When `limit` is a positive integer, only the `limit` closest genes are
    returned. Default (None) returns all genes (full-fidelity behavior).
    """
    genes = matrix["genes"]
    matches = np.flatnonzero(genes == gene)
    if len(matches) == 0:
        raise KeyError(gene)

    query_index = int(matches[0])

    if distance == "euclidean":
        expression = matrix["expression"]
        query = expression[query_index]
        distances = np.linalg.norm(expression - query, axis=1)
    elif distance == "pearson":
        expression = matrix["expression"]
        distances = correlation_distance(expression, expression[query_index])
    elif distance == "spearman":
        rank_expression = matrix["rank_expression"]
        distances = correlation_distance(rank_expression, rank_expression[query_index])
    else:
        raise ValueError(f"Unsupported distance metric: {distance}")

    keep = np.arange(len(genes)) != query_index
    kept_genes = genes[keep]
    kept_distances = distances[keep]

    if limit is not None and limit > 0 and limit < len(kept_distances):
        # argpartition is O(n): grab the `limit` smallest, then sort just those.
        nearest = np.argpartition(kept_distances, limit)[:limit]
        order = nearest[np.argsort(kept_distances[nearest], kind="mergesort")]
    else:
        order = np.argsort(kept_distances, kind="mergesort")

    result_genes = kept_genes[order]
    result_distances = kept_distances[order]

    return [
        {"gene": str(result_gene), "distance": float(result_distance)}
        for result_gene, result_distance in zip(result_genes, result_distances)
    ]
