"""On-demand ("lazy") matrix building from Resolwe.

Lets the fast path cover *every* time series without precomputing matrices for
all of them up front. The first request for a dataset builds its matrix from
data already in Resolwe (plain REST reads, no process queue), and the caller
persists it (to disk locally / to S3 in Lambda) so later requests are instant.

Inputs available on a request:
  - time_series id  -> relation partitions (entity -> position/label)
  - expressions[]   -> expression Data ids the frontend sends; each Data points
                       at a storage object holding {genes: {gene: value}}
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np

from common.resolwe import (
    ResolweError,
    expression_data_ids_for_relation,
    get_data,
    get_relation,
    get_storage,
)

from build_matrix import build_matrix_arrays
from core import build_samples_arrays


class LazyBuildUnavailable(ResolweError):
    """A matrix is missing and cannot be built on demand from the request."""


def fetch_samples(
    time_series_id: int, expression_ids: list[int]
) -> list[tuple[int, dict[str, float]]]:
    """Fetch each sample's expression vector as (entity id, {gene: value})."""
    if not expression_ids:
        raise LazyBuildUnavailable(
            f"Cannot build matrix for time series {time_series_id}: "
            "request did not include any expression ids."
        )

    # Each expression Data object carries its sample (`entity`) and a storage id
    # (`output.exp_json`) holding the gene->value mapping.
    targets: list[tuple[int, int]] = []
    for data in get_data(expression_ids):
        entity = data.get("entity")
        entity_id = entity.get("id") if isinstance(entity, dict) else entity
        storage_id = (data.get("output") or {}).get("exp_json")
        if entity_id is not None and storage_id is not None:
            targets.append((int(entity_id), int(storage_id)))

    def fetch_sample(target: tuple[int, int]) -> tuple[int, dict[str, float]]:
        entity_id, storage_id = target
        storage = get_storage(storage_id)
        genes = (storage.get("json") or {}).get("genes", {})
        return entity_id, {str(g): float(v) for g, v in genes.items()}

    # Per-sample storage objects are large (~12k genes) and each backend GET is
    # several seconds, so fetching them sequentially is what made the first build
    # slow (~36s). Fetch them concurrently (I/O-bound) so total time is bounded by
    # the slowest fetch, not their sum. One-time per dataset.
    with ThreadPoolExecutor(max_workers=min(48, len(targets) or 1)) as pool:
        samples = list(pool.map(fetch_sample, targets))

    if not samples:
        raise LazyBuildUnavailable(
            f"Could not assemble any expression samples for time series {time_series_id}."
        )
    return samples


def build_matrix_from_resolwe(
    time_series_id: int, expression_ids: list[int]
) -> dict[str, np.ndarray]:
    """Fetch relation + per-sample expression vectors and build the matrix."""
    samples = fetch_samples(time_series_id, expression_ids)
    return build_matrix_arrays(get_relation(time_series_id), samples)


def build_samples_from_resolwe(time_series_id: int) -> dict[str, np.ndarray]:
    """Fetch every sample of a time series for the expression time courses."""
    relation = get_relation(time_series_id)
    samples = fetch_samples(time_series_id, expression_data_ids_for_relation(relation))
    return build_samples_arrays(samples)
