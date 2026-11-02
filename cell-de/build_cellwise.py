"""Build the cell-wise counts artifact for one single-cell strain.

Input is a strain folder written by the app's zarr converter
(data_prep/prepare_single_cell_zarr.py): a gene-wise `arrays/expression` zarr
array of raw counts (n_genes x n_cells), `genes/names.json`,
`genes/symbols.json` and `info.json`. The app reads it one gene at a time; the
cell-de lambda needs whole cells, so this rewrites it as a cell-wise CSR matrix
(see core.py for the layout).

    python build_cellwise.py --input ../../../data/single_cell/AX4
    # -> matrices/AX4.npz, then ../aws/upload_matrices.sh cell-de
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

import numpy as np
from scipy import sparse

from core import TARGET_SUM, Cellwise, group_sums, pseudobulk_counts

GENE_BLOCK = 256
UINT16_MAX = np.iinfo(np.uint16).max


def build_cellwise(strain_dir: Path) -> Cellwise:
    """Read a strain's gene-wise zarr and return the cell-wise artifact."""
    import zarr  # build-time only; keeps cellwise_from_csr importable without it

    expression = zarr.open(str(strain_dir / "arrays" / "expression"), mode="r")
    n_genes, n_cells = expression.shape
    if n_genes > UINT16_MAX + 1:
        raise ValueError(f"{n_genes} genes do not fit uint16 gene indices.")

    genes = np.array(json.loads((strain_dir / "genes" / "names.json").read_text()), dtype=str)
    symbols = np.array(json.loads((strain_dir / "genes" / "symbols.json").read_text()), dtype=str)
    if len(genes) != n_genes or len(symbols) != n_genes:
        raise ValueError("Gene names/symbols do not match the expression array.")

    cell_parts, gene_parts, count_parts = [], [], []
    for start in range(0, n_genes, GENE_BLOCK):
        block = np.asarray(expression[start : start + GENE_BLOCK])
        block_genes, block_cells = np.nonzero(block)
        values = block[block_genes, block_cells]
        if not np.array_equal(values, np.round(values)) or values.min(initial=1) < 0:
            raise ValueError("Expression values must be raw, non-negative integer counts.")
        if values.max(initial=0) > UINT16_MAX:
            raise ValueError("Counts above 65535 do not fit uint16.")
        cell_parts.append(block_cells.astype(np.int32))
        gene_parts.append((block_genes + start).astype(np.int32))
        count_parts.append(values.astype(np.uint16))
        print(f"  genes {start + len(block)}/{n_genes}", end="\r", flush=True)
    print()

    matrix = sparse.csr_matrix(
        (np.concatenate(count_parts), (np.concatenate(cell_parts), np.concatenate(gene_parts))),
        shape=(n_cells, n_genes),
        dtype=np.uint16,
    )
    replicate_path = strain_dir / "cells" / "replicate.json"
    replicates = json.loads(replicate_path.read_text()) if replicate_path.exists() else None
    return cellwise_from_csr(matrix, genes, symbols, replicates)


def cellwise_from_csr(
    matrix: sparse.csr_matrix,
    genes: np.ndarray,
    symbols: np.ndarray,
    replicates: list[str | None] | None = None,
) -> Cellwise:
    """Assemble the artifact from a cells x genes CSR matrix of raw counts."""
    matrix = matrix.tocsr()
    matrix.sort_indices()
    n_cells = matrix.shape[0]

    totals = np.asarray(matrix.sum(axis=1, dtype=np.float64)).ravel()
    size_factors = np.zeros(n_cells, dtype=np.float32)
    np.divide(TARGET_SUM, totals, out=size_factors, where=totals > 0, casting="unsafe")

    cellwise: Cellwise = {
        "counts": matrix.data.astype(np.uint16),
        "gene_index": matrix.indices.astype(np.uint16),
        "indptr": matrix.indptr.astype(np.int64),
        "size_factors": size_factors,
        "genes": np.asarray(genes, dtype=str),
        "symbols": np.asarray(symbols, dtype=str),
    }
    gene_sum, gene_sumsq, gene_nnz = group_sums(cellwise, np.arange(n_cells))
    cellwise.update(gene_sum=gene_sum, gene_sumsq=gene_sumsq, gene_nnz=gene_nnz)

    # Biological replicates (cells/replicate.json from data_prep/add_replicates.py), with
    # their count totals so "all other cells" pseudobulks are a subtraction.
    if replicates is not None:
        if len(replicates) != n_cells:
            raise ValueError("cells/replicate.json does not match the number of cells.")
        names = sorted({r for r in replicates if r is not None})
        code_of = {name: code for code, name in enumerate(names)}
        codes = np.array([code_of.get(r, -1) if r is not None else -1 for r in replicates], dtype=np.int8)
        cellwise.update(
            replicate_names=np.asarray(names, dtype=str),
            replicate_codes=codes,
            replicate_counts=np.stack(
                [pseudobulk_counts(cellwise, np.flatnonzero(codes == code)) for code in range(len(names))]
            ),
        )
    return cellwise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, type=Path, help="Strain folder (zarr output)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "matrices",
        help="Where to write <strain>.npz",
    )
    args = parser.parse_args()

    strain = json.loads((args.input / "info.json").read_text())["strain"]
    started = perf_counter()
    cellwise = build_cellwise(args.input)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"{strain}.npz"
    np.savez_compressed(path, **cellwise)
    print(
        f"{strain}: {len(cellwise['indptr']) - 1} cells x {len(cellwise['genes'])} genes, "
        f"{len(cellwise['counts'])} nonzero -> {path} "
        f"({path.stat().st_size / 1e6:.0f} MB, {perf_counter() - started:.0f}s)"
    )


if __name__ == "__main__":
    main()
