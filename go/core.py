"""GO enrichment computation.

Given a precomputed GO index (gene -> propagated terms, term metadata, background
size) and a query gene set, compute over-representation of GO terms and return the
same JSON shape the dictyExpress goenrichment process produces:

    {"tree": {"BP": [...], "CC": [...], "MF": [...]},
     "total_genes": <int>,
     "gene_associations": {<term_id>: [<gene_id>, ...]}}

Each term node: {pval, score, total, matched, term_id, term_name, gene_ids, children}.

Verified against real output (term GO:0032502): with N=9536, total=647, n=2,
matched=2 -> pval = C(647,2)/C(9536,2) = 0.004596739..., score = (2/2)/(647/9536)
= 14.7388. The enrichment math here reproduces both.
"""

from __future__ import annotations

from math import lgamma, exp
from typing import Any

# The three ontology root terms (BP/MF/CC) are excluded from enrichment results —
# every annotated gene maps to them, so they carry no enrichment signal.
GO_ROOTS = frozenset({"GO:0008150", "GO:0003674", "GO:0005575"})

# Aspect codes used in the index (namespace -> short aspect).
ASPECTS = ("BP", "CC", "MF")
NAMESPACE_TO_ASPECT = {
    "biological_process": "BP",
    "cellular_component": "CC",
    "molecular_function": "MF",
}


def _log_choose(n: int, k: int) -> float:
    """log(C(n, k)) via lgamma (stable for large n)."""
    if k < 0 or k > n:
        return float("-inf")
    return lgamma(n + 1) - lgamma(k + 1) - lgamma(n - k + 1)


def hypergeom_sf(matched: int, total_genes: int, term_total: int, query_size: int) -> float:
    """Upper-tail hypergeometric P(X >= matched).

    Population N=total_genes, successes K=term_total, draws n=query_size.
    """
    if matched <= 0:
        return 1.0
    upper = min(query_size, term_total)
    log_denom = _log_choose(total_genes, query_size)
    acc = 0.0
    for i in range(matched, upper + 1):
        log_p = (
            _log_choose(term_total, i)
            + _log_choose(total_genes - term_total, query_size - i)
            - log_denom
        )
        acc += exp(log_p)
    return min(acc, 1.0)


def compute_enrichment(
    index: dict[str, Any], genes: list[str], pval_threshold: float
) -> dict[str, Any]:
    """Compute GO term enrichment for a query gene set.

    `index` (produced by build_index.py) provides:
      - gene_terms:  {gene_id: [propagated term_id, ...]}  (keys = background genes)
      - term_total:  {term_id: int}   background genes annotated (propagated)
      - term_genes:  {term_id: [gene_id, ...]}  for gene_associations
      - term_meta:   {term_id: {"name": str, "aspect": "BP|CC|MF", "parents": [term_id]}}
      - total_genes: int               background size N
    """
    gene_terms = index["gene_terms"]
    term_total = index["term_total"]
    term_meta = index["term_meta"]
    total_genes = int(index["total_genes"])

    # Only query genes that are in the annotated background count as draws.
    query = [g for g in dict.fromkeys(genes) if g in gene_terms]
    n = len(query)
    if n == 0:
        return {"tree": {a: [] for a in ASPECTS}, "total_genes": total_genes, "gene_associations": {}}

    # Candidate terms = those any query gene is (propagated-)annotated to; count matches.
    matched_counts: dict[str, int] = {}
    matched_gene_ids: dict[str, list[str]] = {}
    for gene in query:
        for term in gene_terms[gene]:
            matched_counts[term] = matched_counts.get(term, 0) + 1
            matched_gene_ids.setdefault(term, []).append(gene)

    enriched: dict[str, dict[str, Any]] = {}
    for term, matched in matched_counts.items():
        if term in GO_ROOTS:
            continue
        total = int(term_total.get(term, 0))
        if total == 0:
            continue
        pval = hypergeom_sf(matched, total_genes, total, n)
        if pval > pval_threshold:
            continue
        meta = term_meta.get(term, {})
        enriched[term] = {
            "pval": pval,
            "score": (matched / n) / (total / total_genes),
            "total": total,
            "matched": matched,
            "term_id": term,
            "term_name": meta.get("name", term),
            "gene_ids": sorted(matched_gene_ids[term]),
            "children": [],
            "_aspect": meta.get("aspect"),
        }

    tree = _build_tree(enriched, term_meta)
    gene_associations = {term: index["term_genes"].get(term, []) for term in enriched}

    return {"tree": tree, "total_genes": total_genes, "gene_associations": gene_associations}


def _nearest_enriched_parents(
    term: str, enriched: dict, term_meta: dict, memo: dict[str, list]
) -> list[str]:
    """All nearest enriched ancestors of a term (one per DAG path).

    For each parent edge: the parent itself if enriched, else recurse. A term with
    several enriched parents yields several — the tool duplicates the term under
    each, producing a DAG (not a strict tree).
    """
    if term in memo:
        return memo[term]
    result: list[str] = []
    for parent in term_meta.get(term, {}).get("parents", []):
        hits = [parent] if parent in enriched else _nearest_enriched_parents(
            parent, enriched, term_meta, memo
        )
        for hit in hits:
            if hit not in result:
                result.append(hit)
    memo[term] = result
    return result


_NODE_FIELDS = ("pval", "score", "total", "matched", "term_id", "term_name", "gene_ids")


def _build_tree(enriched: dict[str, dict], term_meta: dict) -> dict[str, list]:
    """Aspect-grouped tree: each enriched term nested once under its nearest
    enriched ancestor (roots have none). Siblings sorted by p-value then term id.

    NOTE: the reference goenrichment tool emits a DAG that *duplicates* terms with
    several enriched parents; that duplication rule is in its closed-source binary
    and is not reproduced here. The enrichment values, term set, and
    gene_associations are identical — only multi-parent display nesting differs.
    """
    memo: dict[str, list] = {}
    children_of: dict[str, list[str]] = {t: [] for t in enriched}
    roots: list[str] = []
    for term in enriched:
        parents = _nearest_enriched_parents(term, enriched, term_meta, memo)
        if parents:
            children_of[parents[0]].append(term)
        else:
            roots.append(term)

    def build_node(term: str) -> dict:
        node = {field: enriched[term][field] for field in _NODE_FIELDS}
        kids = sorted(children_of[term], key=lambda c: (enriched[c]["pval"], c))
        node["children"] = [build_node(c) for c in kids]
        return node

    tree: dict[str, list] = {a: [] for a in ASPECTS}
    for term in sorted(roots, key=lambda c: (enriched[c]["pval"], c)):
        aspect = enriched[term].get("_aspect")
        if aspect in tree:
            tree[aspect].append(build_node(term))
    return tree
