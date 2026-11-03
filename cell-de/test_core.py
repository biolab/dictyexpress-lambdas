"""Tests for cell-de: run with `python -m pytest` from the service directory."""

from __future__ import annotations

import numpy as np
import pytest
from scipy import sparse, stats

from build_cellwise import cellwise_from_csr, time_totals
from core import pseudobulk_counts
from core import (
    MIN_REPLICATE_CELLS,
    TARGET_SUM,
    benjamini_hochberg,
    benjamini_hochberg_log10,
    compare_cells,
    fit_f_dist,
    log_t_sf,
    parse_cells,
    result_to_json,
    trigamma_inverse,
)
from scipy import special


@pytest.fixture(scope="module")
def dataset():
    rng = np.random.default_rng(0)
    n_cells, n_genes = 300, 40
    counts = rng.poisson(0.6, size=(n_cells, n_genes)).astype(np.float64)
    # Gene 0 is a marker of the first 60 cells; gene 1 is never expressed.
    counts[:60, 0] += rng.poisson(8, size=60)
    counts[:, 1] = 0
    genes = np.array([f"DDB_G{i:07d}" for i in range(n_genes)])
    symbols = np.array([f"gene{i}" for i in range(n_genes)])
    cellwise = cellwise_from_csr(sparse.csr_matrix(counts), genes, symbols)

    totals = counts.sum(axis=1, keepdims=True)
    log_normalized = np.log1p(counts / totals * TARGET_SUM)
    return cellwise, log_normalized


def _expected(log_normalized: np.ndarray, selected: np.ndarray, reference: np.ndarray):
    """scanpy's t-test_overestim_var: the reference counts as n_selected cells."""
    group, ref = log_normalized[selected], log_normalized[reference]
    with np.errstate(divide="ignore", invalid="ignore"):
        return stats.ttest_ind_from_stats(
            mean1=group.mean(axis=0),
            std1=group.std(axis=0, ddof=1),
            nobs1=len(selected),
            mean2=ref.mean(axis=0),
            std2=ref.std(axis=0, ddof=1),
            nobs2=len(selected),
            equal_var=False,
        )


@pytest.mark.parametrize(
    "selected, reference",
    [
        (np.arange(60), None),
        # Most cells: the reference sums come from the complement path.
        (np.arange(40, 280), None),
        # Explicit reference that overlaps the selection.
        (np.arange(60), np.arange(30, 200)),
    ],
)
def test_matches_scanpy_overestim_var(dataset, selected, reference):
    cellwise, log_normalized = dataset
    n_cells = len(log_normalized)
    expected_reference = np.setdiff1d(
        np.arange(n_cells) if reference is None else reference, selected
    )
    result = compare_cells(
        cellwise, selected.tolist(), None if reference is None else reference.tolist()
    )
    expected = _expected(log_normalized, selected, expected_reference)

    testable = ~np.isnan(expected.pvalue)
    np.testing.assert_allclose(result["t"][testable], expected.statistic[testable], rtol=1e-6)
    np.testing.assert_allclose(result["pvalue"][testable], expected.pvalue[testable], rtol=1e-6)
    assert result["n_selected"] == len(selected)
    assert result["n_reference"] == len(expected_reference)


def test_rare_gene_absent_from_selection_is_not_significant():
    # Gene 0 is in 1% of cells, none of them among the 100 selected. The
    # selection's variance is 0, so plain Welch calls it a strong hit.
    rng = np.random.default_rng(1)
    counts = rng.poisson(0.6, size=(3000, 30)).astype(np.float64)
    counts[:, 0] = 0
    counts[100::100, 0] = 3
    genes = np.array([f"DDB_G{i:07d}" for i in range(30)])
    cellwise = cellwise_from_csr(sparse.csr_matrix(counts), genes, genes)

    log_normalized = np.log1p(counts / counts.sum(axis=1, keepdims=True) * TARGET_SUM)
    welch = stats.ttest_ind(log_normalized[:100, 0], log_normalized[100:, 0], equal_var=False)
    result = compare_cells(cellwise, range(100))

    assert welch.pvalue < 1e-3
    assert result["pvalue"][0] > 0.05


def test_marker_gene_ranks_first(dataset):
    cellwise, _ = dataset
    result = compare_cells(cellwise, range(60))

    assert np.argmin(result["pvalue"]) == 0
    assert result["log2_fold_change"][0] > 2
    assert result["pct_selected"][0] == 1.0
    assert result["fdr"][0] < 1e-10


def test_unexpressed_gene_is_untestable_not_nan(dataset):
    cellwise, _ = dataset
    result = compare_cells(cellwise, range(60))

    assert result["pvalue"][1] == 1.0
    assert result["log2_fold_change"][1] == 0.0
    for key in ("t", "pvalue", "fdr", "neg_log10_pvalue", "log2_fold_change"):
        assert np.all(np.isfinite(result[key]))


def test_fdr_is_monotone_in_pvalue(dataset):
    cellwise, _ = dataset
    result = compare_cells(cellwise, range(100))
    order = np.argsort(result["pvalue"])

    assert np.all(np.diff(result["fdr"][order]) >= 0)
    assert np.all(result["fdr"] >= result["pvalue"])


@pytest.mark.parametrize(
    "t, df",
    [(3.0, 10.0), (50.0, 100.0), (40.0, 30000.0), (435.0, 25000.0), (60.0, 5.0)],
)
def test_log_t_sf_matches_high_precision(t, df):
    mpmath = pytest.importorskip("mpmath")
    mpmath.mp.dps = 50
    x = mpmath.mpf(df) / (df + mpmath.mpf(t) ** 2)
    expected = float(mpmath.log(mpmath.betainc(df / 2, 0.5, 0, x, regularized=True) / 2))

    got = log_t_sf(np.array([t]), np.array([df]))[0]

    assert np.isfinite(got)
    assert got == pytest.approx(expected, rel=1e-9)


def test_strong_markers_have_finite_neg_log10_pvalue():
    # One gene perfectly separates the groups: p underflows, -log10 p must not.
    rng = np.random.default_rng(2)
    counts = rng.poisson(0.6, size=(4000, 30)).astype(np.float64)
    counts[:2000, 0] = rng.poisson(30, size=2000)
    counts[2000:, 0] = 0
    genes = np.array([f"DDB_G{i:07d}" for i in range(30)])
    result = compare_cells(cellwise_from_csr(sparse.csr_matrix(counts), genes, genes), range(2000))

    assert result["pvalue"][0] == 0.0
    assert np.isfinite(result["neg_log10_pvalue"][0])
    assert result["neg_log10_pvalue"][0] > 1000


def test_cohens_d_uses_the_average_group_variance(dataset):
    cellwise, log_normalized = dataset
    selected, reference = np.arange(60), np.arange(60, 300)
    result = compare_cells(cellwise, selected.tolist())

    group, ref = log_normalized[selected], log_normalized[reference]
    sd = np.sqrt((group.var(axis=0, ddof=1) + ref.var(axis=0, ddof=1)) / 2)
    expected = np.divide(
        group.mean(axis=0) - ref.mean(axis=0), sd, out=np.zeros_like(sd), where=sd > 0
    )
    np.testing.assert_allclose(result["cohens_d"], expected, rtol=1e-6, atol=1e-12)
    assert result["cohens_d"][1] == 0.0  # never expressed


def test_cohens_d_does_not_grow_with_the_number_of_cells(dataset):
    # Same gene-0 marker population, 30 vs 60 selected cells: t grows, d doesn't.
    cellwise, _ = dataset
    small = compare_cells(cellwise, range(30), range(60, 300))
    large = compare_cells(cellwise, range(60), range(60, 300))

    assert large["t"][0] > 1.3 * small["t"][0]
    assert large["cohens_d"][0] == pytest.approx(small["cohens_d"][0], rel=0.2)


def test_log_fdr_matches_linear_fdr(dataset):
    cellwise, _ = dataset
    result = compare_cells(cellwise, range(100))
    expected = benjamini_hochberg(result["pvalue"])

    np.testing.assert_allclose(10 ** -result["neg_log10_fdr"], expected, rtol=1e-9)
    assert np.all(np.isfinite(benjamini_hochberg_log10(np.array([5000.0, 3.0, 0.1]))))


@pytest.fixture(scope="module")
def replicated():
    """Three replicates of 400 cells; gene 0 marks the first 100 cells of each, and
    replicate 'c' is shifted up for every gene (a batch effect the test must absorb)."""
    rng = np.random.default_rng(3)
    blocks, labels = [], []
    for name, scale in [("a", 1.0), ("b", 1.0), ("c", 1.5)]:
        counts = rng.poisson(0.8 * scale, size=(400, 200)).astype(np.float64)
        counts[:100, 0] += rng.poisson(6, size=100)
        blocks.append(counts)
        labels += [name] * 400
    labels[-5:] = [None] * 5  # unassigned cells
    counts = np.vstack(blocks)
    genes = np.array([f"DDB_G{i:07d}" for i in range(200)])
    cellwise = cellwise_from_csr(sparse.csr_matrix(counts), genes, genes, labels)
    marker_cells = np.concatenate([np.arange(100) + 400 * r for r in range(3)])
    return cellwise, marker_cells


def test_trigamma_inverse_inverts_trigamma():
    y = np.array([1e-3, 0.05, 1.0, 12.0])
    np.testing.assert_allclose(special.polygamma(1, trigamma_inverse(y)), y, rtol=1e-7)


def test_fit_f_dist_recovers_prior():
    # Variances drawn from the limma model: s2 ~ s0^2 * F(df, d0).
    rng = np.random.default_rng(4)
    variances = 0.2 * rng.f(2, 8, size=20000)
    d0, s0_squared = fit_f_dist(variances, 2)
    assert d0 == pytest.approx(8, rel=0.15)
    assert s0_squared == pytest.approx(0.2, rel=0.1)


def test_replicates_are_stored_with_their_counts(replicated):
    cellwise, _ = replicated
    assert cellwise["replicate_names"].tolist() == ["a", "b", "c"]
    assert np.bincount(cellwise["replicate_codes"] + 1).tolist() == [5, 400, 400, 395]
    a_cells = np.flatnonzero(cellwise["replicate_codes"] == 0)
    np.testing.assert_allclose(
        cellwise["replicate_counts"][0], pseudobulk_counts(cellwise, a_cells)
    )


def test_replicate_test_finds_the_marker(replicated):
    cellwise, marker_cells = replicated
    result = compare_cells(cellwise, marker_cells.tolist())
    test = result["replicate_test"]

    assert test["status"] == "ok"
    assert [r["used"] for r in test["replicates"]] == [True, True, True]
    assert np.nanargmin(test["pvalue"]) == 0
    assert test["fdr"][0] < 0.05
    assert test["log2_fold_change"][0] > 1


def test_replicate_test_is_quiet_on_random_cells(replicated):
    cellwise, _ = replicated
    rng = np.random.default_rng(5)
    cells = np.sort(rng.choice(np.arange(1, 1200, 2), 150, replace=False))  # skips marker
    test = compare_cells(cellwise, cells.tolist())["replicate_test"]

    assert test["status"] == "ok"
    assert np.nansum(test["fdr"] < 0.05) == 0


def test_same_time_reference_uses_time_totals(replicated):
    cellwise, _ = replicated
    times = [("t0", "t1", "t2")[cell % 3] for cell in range(1200)]
    with_times = {**cellwise, **time_totals(cellwise, times)}
    rng = np.random.default_rng(6)
    selected = np.sort(rng.choice(np.flatnonzero(np.array(times) == "t1"), 120, replace=False))
    whole = [cell for cell in range(1200) if times[cell] in ("t1", "t2")]  # includes selected
    partial = whole[::2]

    for reference in (whole, partial):
        expected = compare_cells(cellwise, selected.tolist(), reference)
        result = compare_cells(with_times, selected.tolist(), reference)
        for key in ("cohens_d", "neg_log10_pvalue", "pct_reference"):
            np.testing.assert_allclose(result[key], expected[key], rtol=1e-9, atol=1e-12)
        np.testing.assert_allclose(
            result["replicate_test"]["pvalue"], expected["replicate_test"]["pvalue"], rtol=1e-9
        )


def test_replicate_test_needs_cells_in_two_replicates(replicated):
    cellwise, _ = replicated
    # All selected cells come from replicate a.
    test = compare_cells(cellwise, list(range(50)))["replicate_test"]

    assert test["status"].startswith("Fewer than 2 replicates")
    assert "pvalue" not in test
    assert [r["n_selected"] for r in test["replicates"]] == [50, 0, 0]
    assert MIN_REPLICATE_CELLS == 10


def test_replicate_results_serialize_with_nulls(replicated):
    cellwise, marker_cells = replicated
    body = result_to_json(compare_cells(cellwise, marker_cells.tolist()))

    assert body["replicate_test"]["status"] == "ok"
    assert len(body["replicate_pvalue"]) == 200
    assert all(v is None or isinstance(v, float) for v in body["replicate_fdr"])
    assert "pvalue" not in body["replicate_test"]


def test_strain_without_replicates_says_so(dataset):
    cellwise, _ = dataset
    body = result_to_json(compare_cells(cellwise, range(60)))
    assert body["replicate_test"] == {"status": "This strain has no replicate labels."}
    assert "replicate_pvalue" not in body


def test_parse_cells_validates_indices():
    assert parse_cells([5, 3, 3, 4], 10).tolist() == [3, 4, 5]
    with pytest.raises(ValueError):
        parse_cells([0, 1, 10], 10)
    with pytest.raises(ValueError):
        parse_cells([-1, 1, 2], 10)


def test_groups_need_enough_cells(dataset):
    cellwise, log_normalized = dataset
    n_cells = len(log_normalized)
    with pytest.raises(ValueError, match="selected"):
        compare_cells(cellwise, [0, 1])
    with pytest.raises(ValueError, match="reference"):
        compare_cells(cellwise, range(n_cells - 2))
    with pytest.raises(ValueError, match="reference"):
        # The reference is all selected cells, so nothing is left after removing them.
        compare_cells(cellwise, range(10), range(5))


def test_result_to_json_is_columnar(dataset):
    cellwise, _ = dataset
    body = result_to_json(compare_cells(cellwise, range(60)))

    assert body["test"] == "t-test_overestim_var"
    assert body["genes"][0] == "DDB_G0000000"
    assert len(body["pvalue"]) == len(body["genes"]) == 40
    assert all(isinstance(value, float) for value in body["log2_fold_change"])
