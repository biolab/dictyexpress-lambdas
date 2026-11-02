"""Differential expression of a user-selected group of cells vs a reference.

A strain's single-cell counts are stored on disk/S3 as a cell-wise `.npz`
artifact (built by build_cellwise.py from the app's gene-wise zarr):

    counts       uint16 (nnz,)        raw UMI counts, CSR data
    gene_index   uint16 (nnz,)        CSR column (gene) indices
    indptr       int64  (n_cells+1,)  CSR row (cell) pointers
    size_factors float32 (n_cells,)   TARGET_SUM / total counts of the cell
    genes        str    (n_genes,)    gene IDs (DDB_G...)
    symbols      str    (n_genes,)    gene symbols
    gene_sum     float64 (n_genes,)   sum of log-normalized values over all cells
    gene_sumsq   float64 (n_genes,)   sum of squared log-normalized values
    gene_nnz     int64  (n_genes,)    number of cells expressing the gene
    replicate_names  str   (n_replicates,)          biological replicates (optional)
    replicate_codes  int8  (n_cells,)               index into replicate_names, -1 if none
    replicate_counts float64 (n_replicates, n_genes) raw counts summed per replicate

Values are normalized the scanpy way (scale each cell to TARGET_SUM counts, then
log1p) and compared with scanpy's `t-test_overestim_var`: Welch's t-test with
the reference group's variance scaled by the selected group's size instead of
its own. Plain Welch calls a rare gene absent from every selected cell (zero
variance) highly significant; on AX4 it gave ~1,200 genes at FDR < 0.05 for 200
random cells, this variant none.

Genes are meant to be ranked by `cohens_d`, an effect size that doesn't grow with
the number of cells. The p-values treat every cell as an independent replicate and
the cells were picked from the same data, so they come out absurdly small (often
below 1e-1000) and are returned for reference only. For one comparison, ranking by
d and by the t statistic agree exactly: t is d scaled by a constant.

The reference defaults to all other cells of the strain. Then only the smaller
of the two groups is summed and the other is the precomputed totals minus it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO, Iterable

import numpy as np
from scipy import special, stats

Cellwise = dict[str, np.ndarray]

TARGET_SUM = 1e4
# Strain names become S3 keys/file names, so only allow what the data folders use
# (AX4, acaA-, acaA-pkaC).
STRAIN_PATTERN = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
# Fewer cells than this in either group gives no usable variance estimate.
MIN_GROUP_CELLS = 3
# Cells summed per batch; bounds the per-request temporaries to a few hundred MB.
BATCH_CELLS = 4096
# Pseudocount for the fold change of means, as in scanpy's rank_genes_groups.
FOLD_CHANGE_EPS = 1e-9
# A replicate takes part in the pseudobulk test only with this many cells in both groups,
# and the test needs at least MIN_REPLICATES of them.
MIN_REPLICATE_CELLS = 10
MIN_REPLICATES = 2
# Genes need this many counts in a pseudobulk sample of median size (edgeR filterByExpr).
MIN_PSEUDOBULK_COUNT = 10


def load_cellwise(source: Path | str | BinaryIO) -> Cellwise:
    """Load a cell-wise counts artifact from a NumPy .npz file."""
    loaded = np.load(source, allow_pickle=False)
    return {key: loaded[key] for key in loaded.files}


def group_sums(
    cellwise: Cellwise, cells: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-gene sum, sum of squares and nonzero count of log-normalized values."""
    n_genes = len(cellwise["genes"])
    indptr = cellwise["indptr"]
    counts = cellwise["counts"]
    gene_index = cellwise["gene_index"]
    size_factors = cellwise["size_factors"]

    total = np.zeros(n_genes, dtype=np.float64)
    total_sq = np.zeros(n_genes, dtype=np.float64)
    nnz = np.zeros(n_genes, dtype=np.int64)

    for batch_start in range(0, len(cells), BATCH_CELLS):
        batch = cells[batch_start : batch_start + BATCH_CELLS]
        starts = indptr[batch]
        lengths = indptr[batch + 1] - starts
        if lengths.sum() == 0:
            continue
        # Positions of every nonzero entry of the batch's CSR rows.
        offsets = np.cumsum(lengths) - lengths
        positions = np.arange(lengths.sum()) + np.repeat(starts - offsets, lengths)

        values = np.log1p(
            counts[positions] * np.repeat(size_factors[batch].astype(np.float64), lengths)
        )
        genes = gene_index[positions]
        total += np.bincount(genes, weights=values, minlength=n_genes)
        total_sq += np.bincount(genes, weights=values * values, minlength=n_genes)
        nnz += np.bincount(genes, minlength=n_genes)

    return total, total_sq, nnz


def pseudobulk_counts(cellwise: Cellwise, cells: np.ndarray) -> np.ndarray:
    """Raw counts per gene summed over the given cells."""
    n_genes = len(cellwise["genes"])
    indptr = cellwise["indptr"]
    total = np.zeros(n_genes, dtype=np.float64)
    for batch_start in range(0, len(cells), BATCH_CELLS):
        batch = cells[batch_start : batch_start + BATCH_CELLS]
        starts = indptr[batch]
        lengths = indptr[batch + 1] - starts
        if lengths.sum() == 0:
            continue
        offsets = np.cumsum(lengths) - lengths
        positions = np.arange(lengths.sum()) + np.repeat(starts - offsets, lengths)
        total += np.bincount(
            cellwise["gene_index"][positions],
            weights=cellwise["counts"][positions],
            minlength=n_genes,
        )
    return total


def parse_cells(cells: Iterable[int], n_cells: int, name: str = "Cell") -> np.ndarray:
    """Validate requested cell indices and return them sorted and unique."""
    parsed = np.unique(np.asarray(list(cells), dtype=np.int64))
    if len(parsed) and (parsed[0] < 0 or parsed[-1] >= n_cells):
        raise ValueError(f"{name} indices must be between 0 and {n_cells - 1}.")
    return parsed


def _check_group_size(n: int, name: str) -> None:
    if n < MIN_GROUP_CELLS:
        raise ValueError(f"The {name} cells need at least {MIN_GROUP_CELLS} cells, got {n}.")


def _mean_var(total: np.ndarray, total_sq: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    mean = total / n
    var = np.maximum(total_sq - n * mean * mean, 0.0) / (n - 1)
    return mean, var


def log_t_sf(t: np.ndarray, df: np.ndarray) -> np.ndarray:
    """log P(T > t) for t >= 0, without underflow.

    scipy's t.logsf is log(sf), so it hits -inf once sf drops below ~1e-308, which
    thousands of cells reach easily. There, use the exact identity
    sf = I_x(df/2, 1/2) / 2 with x = df / (df + t^2) and
    I_x(a, b) = x^a (1-x)^b / (a B(a, b)) * 2F1(a+b, 1; a+1; x)  (DLMF 8.17.8),
    whose log is finite; x is tiny there, so 2F1 converges at once.
    """
    log_sf = stats.t.logsf(t, df)
    tail = ~np.isfinite(log_sf) | (log_sf < -600)
    if np.any(tail):
        tt, nu = t[tail], df[tail]
        a = nu / 2.0
        x = nu / (nu + tt * tt)
        log_sf[tail] = (
            np.log(0.5)
            + a * np.log(x)
            + 0.5 * np.log1p(-x)
            - np.log(a)
            - special.betaln(a, 0.5)
            + np.log(special.hyp2f1(a + 0.5, 1.0, a + 1.0, x))
        )
    return log_sf


def trigamma_inverse(y: np.ndarray | float) -> np.ndarray:
    """Solve trigamma(x) = y for x > 0 (limma::trigammaInverse, Newton's method)."""
    y = np.atleast_1d(np.asarray(y, dtype=np.float64))
    x = 0.5 + 1.0 / y
    for _ in range(50):
        tri = special.polygamma(1, x)
        step = tri * (1.0 - tri / y) / special.polygamma(2, x)
        x = x + step
        if np.max(-step / x) < 1e-8:
            break
    x = np.where(y > 1e7, 1.0 / np.sqrt(y), x)
    return np.where(y < 1e-6, 1.0 / y, x)


def fit_f_dist(variances: np.ndarray, df: float) -> tuple[float, float]:
    """Prior (d0, s0^2) for the gene variances, as limma::fitFDist (eBayes, robust=FALSE)."""
    x = np.maximum(variances, 0.0)
    median = np.median(x)
    x = np.maximum(x, 1e-5 * (median if median > 0 else 1.0))
    e = np.log(x) - special.digamma(df / 2.0) + np.log(df / 2.0)
    e_mean = e.mean()
    e_var = e.var(ddof=1) - special.polygamma(1, df / 2.0)
    if e_var > 0:
        d0 = 2.0 * float(trigamma_inverse(e_var)[0])
        s0_squared = float(np.exp(e_mean + special.digamma(d0 / 2.0) - np.log(d0 / 2.0)))
    else:
        d0, s0_squared = np.inf, float(np.exp(e_mean))
    return d0, s0_squared


def replicate_test(
    cellwise: Cellwise, selected: np.ndarray, reference: np.ndarray | None
) -> dict[str, object]:
    """Pseudobulk test across biological replicates.

    Per replicate, the raw counts of its selected and its reference cells are summed into
    two pseudobulk samples. This is limma's paired design (~ replicate + group) on voom
    log-CPM: per gene, the selected-vs-reference log-CPM differences are tested against 0
    with a t statistic whose variance is shrunk towards a common prior (empirical Bayes).
    The variation it accounts for is between replicates, not between cells.
    """
    n_genes = len(cellwise["genes"])
    if "replicate_codes" not in cellwise:
        return {"status": "This strain has no replicate labels."}
    codes = cellwise["replicate_codes"]
    names = cellwise["replicate_names"]
    replicate_sizes = np.bincount(codes[codes >= 0], minlength=len(names))

    replicates, selected_counts, reference_counts = [], [], []
    for code, name in enumerate(names):
        sel_cells = selected[codes[selected] == code]
        if reference is None:
            n_ref = int(replicate_sizes[code]) - len(sel_cells)
        else:
            ref_cells = reference[codes[reference] == code]
            n_ref = len(ref_cells)
        replicates.append({"name": str(name), "n_selected": len(sel_cells), "n_reference": n_ref})
        if len(sel_cells) < MIN_REPLICATE_CELLS or n_ref < MIN_REPLICATE_CELLS:
            replicates[-1]["used"] = False
            continue
        replicates[-1]["used"] = True
        sel_sum = pseudobulk_counts(cellwise, sel_cells)
        if reference is None:
            ref_sum = cellwise["replicate_counts"][code] - sel_sum
        else:
            ref_sum = pseudobulk_counts(cellwise, ref_cells)
        selected_counts.append(sel_sum)
        reference_counts.append(ref_sum)

    k = len(selected_counts)
    if k < MIN_REPLICATES:
        return {
            "status": f"Fewer than {MIN_REPLICATES} replicates have at least "
            f"{MIN_REPLICATE_CELLS} selected and {MIN_REPLICATE_CELLS} reference cells.",
            "replicates": replicates,
        }

    selected_counts = np.array(selected_counts)
    reference_counts = np.array(reference_counts)
    samples = np.vstack([selected_counts, reference_counts])  # (2k, genes)
    libraries = samples.sum(axis=1, keepdims=True)
    log_cpm = np.log2((samples + 0.5) / (libraries + 1.0) * 1e6)  # voom
    # Keep genes expressed in at least k of the 2k samples (edgeR filterByExpr).
    cpm_cutoff = MIN_PSEUDOBULK_COUNT / np.median(libraries) * 1e6
    expressed = (samples / libraries * 1e6 >= cpm_cutoff).sum(axis=0) >= k
    kept = np.flatnonzero(expressed)

    differences = (log_cpm[:k] - log_cpm[k:])[:, kept]
    mean_difference = differences.mean(axis=0)
    df = k - 1
    variances = differences.var(axis=0, ddof=1)
    d0, s0_squared = fit_f_dist(variances, df)
    if np.isfinite(d0):
        posterior = (d0 * s0_squared + df * variances) / (d0 + df)
        df_total = min(d0 + df, df * len(kept))  # limma caps at the pooled residual df
    else:
        posterior = np.full_like(variances, s0_squared)
        df_total = df * len(kept)
    t_stat = mean_difference / np.sqrt(posterior / k)
    log_p = np.minimum(
        np.log(2.0) + log_t_sf(np.abs(t_stat), np.full_like(t_stat, df_total)), 0.0
    )

    pvalue = np.full(n_genes, np.nan)
    fdr = np.full(n_genes, np.nan)
    log2_fold_change = np.full(n_genes, np.nan)
    pvalue[kept] = np.exp(log_p)
    fdr[kept] = benjamini_hochberg(np.exp(log_p))
    log2_fold_change[kept] = mean_difference
    return {
        "status": "ok",
        "replicates": replicates,
        "n_genes_tested": len(kept),
        "df_prior": d0 if np.isfinite(d0) else None,
        "pvalue": pvalue,
        "fdr": fdr,
        "log2_fold_change": log2_fold_change,
    }


def benjamini_hochberg_log10(neg_log10_p: np.ndarray) -> np.ndarray:
    """-log10 of Benjamini-Hochberg adjusted p-values, from -log10 p (no underflow)."""
    n = len(neg_log10_p)
    order = np.argsort(-neg_log10_p)  # ascending p
    ranked = neg_log10_p[order] - np.log10(n / np.arange(1, n + 1))
    # adjusted p = running min from the largest p down -> running max of -log10.
    adjusted = np.maximum.accumulate(ranked[::-1])[::-1]
    result = np.empty(n, dtype=np.float64)
    result[order] = np.maximum(adjusted, 0.0)
    return result


def benjamini_hochberg(pvalues: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values."""
    n = len(pvalues)
    order = np.argsort(pvalues)
    ranked = pvalues[order] * n / np.arange(1, n + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    result = np.empty(n, dtype=np.float64)
    result[order] = np.minimum(adjusted, 1.0)
    return result


def compare_cells(
    cellwise: Cellwise,
    cells: Iterable[int],
    reference: Iterable[int] | None = None,
) -> dict[str, np.ndarray | int]:
    """Test every gene: selected cells vs reference cells (default: all others).

    Selected cells are dropped from an explicit reference, so a caller can pass
    e.g. all cells of the same time points without excluding the selection.
    """
    n_cells = len(cellwise["indptr"]) - 1
    selected = parse_cells(cells, n_cells)
    n_selected = len(selected)
    _check_group_size(n_selected, "selected")

    ref_cells = None
    if reference is None:
        n_ref = n_cells - n_selected
        _check_group_size(n_ref, "reference")
        # Sum whichever group is smaller and derive the other from the totals.
        if n_selected <= n_ref:
            sel_sum, sel_sq, sel_nnz = group_sums(cellwise, selected)
            ref_sum = cellwise["gene_sum"] - sel_sum
            ref_sq = cellwise["gene_sumsq"] - sel_sq
            ref_nnz = cellwise["gene_nnz"] - sel_nnz
        else:
            rest = np.setdiff1d(np.arange(n_cells), selected, assume_unique=True)
            ref_sum, ref_sq, ref_nnz = group_sums(cellwise, rest)
            sel_sum = cellwise["gene_sum"] - ref_sum
            sel_sq = cellwise["gene_sumsq"] - ref_sq
            sel_nnz = cellwise["gene_nnz"] - ref_nnz
    else:
        ref_cells = np.setdiff1d(
            parse_cells(reference, n_cells, "Reference cell"), selected, assume_unique=True
        )
        n_ref = len(ref_cells)
        _check_group_size(n_ref, "reference")
        sel_sum, sel_sq, sel_nnz = group_sums(cellwise, selected)
        ref_sum, ref_sq, ref_nnz = group_sums(cellwise, ref_cells)

    replicates = replicate_test(cellwise, selected, ref_cells)

    mean_sel, var_sel = _mean_var(sel_sum, sel_sq, n_selected)
    mean_ref, var_ref = _mean_var(ref_sum, ref_sq, n_ref)

    # t-test_overestim_var: the reference counts as having n_selected cells.
    se_sel = var_sel / n_selected
    se_ref = var_ref / n_selected
    se2 = se_sel + se_ref
    testable = se2 > 0

    t_stat = np.zeros_like(se2)
    t_stat[testable] = (mean_sel - mean_ref)[testable] / np.sqrt(se2[testable])
    # Welch-Satterthwaite degrees of freedom.
    df = np.ones_like(se2)
    df[testable] = se2[testable] ** 2 / (
        (se_sel[testable] ** 2 + se_ref[testable] ** 2) / (n_selected - 1)
    )

    # Two-sided p in log space: with thousands of cells p is often far below 1e-308,
    # so `pvalue` underflows to 0 but `neg_log10_pvalue` stays exact.
    log_p = np.zeros_like(se2)
    log_p[testable] = np.minimum(
        np.log(2.0) + log_t_sf(np.abs(t_stat[testable]), df[testable]), 0.0
    )
    pvalue = np.exp(log_p)

    # Cohen's d with the standard deviation averaged over the two groups, as in
    # scran's scoreMarkers (OSCA), so a small selection doesn't dominate it.
    pooled_sd = np.sqrt((var_sel + var_ref) / 2.0)
    cohens_d = np.zeros_like(pooled_sd)
    has_sd = pooled_sd > 0
    cohens_d[has_sd] = (mean_sel - mean_ref)[has_sd] / pooled_sd[has_sd]

    neg_log10_pvalue = -log_p / np.log(10.0)

    log2_fold_change = np.log2(
        (np.expm1(mean_sel) + FOLD_CHANGE_EPS) / (np.expm1(mean_ref) + FOLD_CHANGE_EPS)
    )

    return {
        "n_selected": n_selected,
        "n_reference": n_ref,
        "genes": cellwise["genes"],
        "symbols": cellwise["symbols"],
        "log2_fold_change": log2_fold_change,
        "cohens_d": cohens_d,
        "t": t_stat,
        "pvalue": pvalue,
        "neg_log10_pvalue": neg_log10_pvalue,
        "fdr": benjamini_hochberg(pvalue),
        "neg_log10_fdr": benjamini_hochberg_log10(neg_log10_pvalue),
        "mean_selected": mean_sel,
        "mean_reference": mean_ref,
        "pct_selected": sel_nnz / n_selected,
        "pct_reference": ref_nnz / n_ref,
        "replicate_test": replicates,
    }


def _rounded(values: np.ndarray) -> list[float]:
    """Six significant digits keep the ~13k-gene response small; never NaN/inf."""
    finite = np.nan_to_num(values.astype(np.float64), nan=0.0, posinf=1e300, neginf=-1e300)
    return [float(f"{value:.6g}") for value in finite]


def result_to_json(result: dict[str, np.ndarray | int]) -> dict[str, object]:
    """Columnar JSON body: one list per field, aligned by gene."""
    body: dict[str, object] = {
        "test": "t-test_overestim_var",
        "n_selected": int(result["n_selected"]),
        "n_reference": int(result["n_reference"]),
        "genes": [str(gene) for gene in result["genes"]],
        "symbols": [str(symbol) for symbol in result["symbols"]],
    }
    for key, values in result.items():
        if key not in body and key != "replicate_test":
            body[key] = _rounded(np.asarray(values))

    # Replicate test: arrays with null for genes it didn't test; absent if it didn't run.
    replicate = dict(result["replicate_test"])
    for key in ("pvalue", "fdr", "log2_fold_change"):
        if key in replicate:
            values = np.asarray(replicate[key], dtype=np.float64)
            body[f"replicate_{key}"] = [
                None if np.isnan(v) else float(f"{v:.6g}") for v in values
            ]
            del replicate[key]
    body["replicate_test"] = replicate
    return body
