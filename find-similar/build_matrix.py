"""Build a precomputed find-similar matrix for one dictyExpress time series."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np


def average_ranks(values: np.ndarray) -> np.ndarray:
    """Return 1-based average ranks, matching Spearman tie handling."""
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(len(values), dtype=np.float64)

    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and sorted_values[end] == sorted_values[start]:
            end += 1

        rank = (start + 1 + end) / 2.0
        ranks[order[start:end]] = rank
        start = end

    return ranks


def is_const(values: np.ndarray) -> bool:
    """Return True if a gene profile is approximately constant."""
    mn = float(np.min(values))
    mx = float(np.max(values))
    if mn + mx == 0.0:
        return mn == mx
    return (mx - mn) / abs(mx + mn) < 1.0e-6


def load_relation(path: Path, time_series_id: int) -> dict:
    """Load one relation from a relation object or relation-list JSON file."""
    payload = json.loads(path.read_text())
    if isinstance(payload, dict):
        if payload.get("id") != time_series_id:
            raise ValueError(
                f"Relation file contains id {payload.get('id')}, expected {time_series_id}."
            )
        return payload

    for relation in payload:
        if relation.get("id") == time_series_id:
            return relation

    raise ValueError(f"Relation {time_series_id} was not found in {path}.")


def load_expression_file(path: Path) -> tuple[int, dict[str, float]]:
    """Load one expression JSON file and return entity id plus gene values."""
    payload = json.loads(path.read_text())
    entity = payload.get("entity")
    if isinstance(entity, dict):
        entity = entity["id"]

    genes = payload.get("json", {}).get("genes")
    if genes is None and "genes" in payload:
        genes = payload["genes"]

    if entity is None or genes is None:
        raise ValueError(f"{path} does not contain an entity id and json.genes.")

    return int(entity), {str(gene): float(value) for gene, value in genes.items()}


def build_matrix_arrays(
    relation: dict, samples: list[tuple[int, dict[str, float]]]
) -> dict[str, np.ndarray]:
    """Build matrix arrays from in-memory per-sample expression vectors.

    `samples` is a list of `(entity_id, {gene: value})`. This is the shared
    core used by both the offline file-based builder and the on-demand
    Resolwe builder, so they always produce identical artifacts.
    """
    entity_to_position = {
        int(partition["entity"]): int(partition["position"])
        for partition in relation["partitions"]
    }
    position_to_label = {
        int(partition["position"]): str(partition["label"])
        for partition in relation["partitions"]
    }

    expressions_by_position: dict[int, list[dict[str, float]]] = defaultdict(list)
    for entity, genes in samples:
        if entity not in entity_to_position:
            continue

        expressions_by_position[entity_to_position[entity]].append(genes)

    missing = sorted(set(position_to_label).difference(expressions_by_position))
    if missing:
        raise ValueError(f"Missing expression JSON for time positions: {missing}.")

    shared_genes: set[str] | None = None
    for replicate_group in expressions_by_position.values():
        for replicate in replicate_group:
            replicate_genes = set(replicate)
            shared_genes = (
                replicate_genes
                if shared_genes is None
                else shared_genes.intersection(replicate_genes)
            )

    if not shared_genes:
        raise ValueError("No genes are shared across all expression files.")

    genes = np.array(sorted(shared_genes), dtype=str)
    labels = np.array(
        [position_to_label[position] for position in sorted(expressions_by_position)],
        dtype=str,
    )
    expression = np.zeros((len(genes), len(labels)), dtype=np.float64)

    gene_to_index = {gene: index for index, gene in enumerate(genes)}
    for column, position in enumerate(sorted(expressions_by_position)):
        replicates = expressions_by_position[position]
        for replicate in replicates:
            for gene in genes:
                expression[gene_to_index[gene], column] += replicate[gene]
        expression[:, column] /= len(replicates)

    non_const = np.array([not is_const(row) for row in expression], dtype=bool)
    genes = genes[non_const]
    expression = expression[non_const]

    rank_expression = np.vstack([average_ranks(row) for row in expression]).astype(
        np.float64
    )

    return {
        "genes": genes,
        "expression": expression,
        "rank_expression": rank_expression,
        "timepoints": labels,
    }


def build_matrix(relation: dict, expressions_dir: Path) -> dict[str, np.ndarray]:
    """Build matrix arrays for one time series from a directory of JSON files."""
    samples = [
        load_expression_file(path) for path in sorted(expressions_dir.glob("*.json"))
    ]
    return build_matrix_arrays(relation, samples)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relation", required=True, type=Path)
    parser.add_argument("--expressions-dir", required=True, type=Path)
    parser.add_argument("--time-series-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    relation = load_relation(args.relation, args.time_series_id)
    matrix = build_matrix(relation, args.expressions_dir)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.output, **matrix)

    print(
        f"Wrote {args.output} with "
        f"{matrix['expression'].shape[0]} genes x {matrix['expression'].shape[1]} timepoints."
    )


if __name__ == "__main__":
    main()
