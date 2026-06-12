"""Build a GO enrichment index from a GAF (annotations) + OBO (ontology).

The index is the offline-precomputed artifact (analogous to find-similar's matrix):
per (source, species) it captures everything the per-request enrichment needs, so
the Lambda just runs the hypergeometric test for a query gene set.

Index contents (see go/core.py for how they're used):
  gene_terms:  {gene_id: [propagated term_id, ...]}   keys = background (annotated) genes
  term_total:  {term_id: int}                          background genes annotated (propagated)
  term_genes:  {term_id: [gene_id, ...]}               for gene_associations
  term_meta:   {term_id: {"name", "aspect", "parents": [direct parent term_id]}}
  total_genes: int                                     background size N

Propagation follows the GO true-path rule over is_a + part_of (transitive closure).
term_meta["parents"] are DIRECT parents only (used to nest the result tree).
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from core import NAMESPACE_TO_ASPECT


def _open(path: Path | str):
    path = str(path)
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path, "rt")


def parse_obo(path: Path | str) -> tuple[dict[str, dict], dict[str, str]]:
    """Parse an OBO file into term metadata + alt_id->primary id mapping.

    Returns (terms, alt_ids) where terms[id] = {name, namespace, parents:set, obsolete}.
    parents are DIRECT is_a + part_of parents.
    """
    terms: dict[str, dict] = {}
    alt_ids: dict[str, str] = {}
    cur: dict | None = None
    in_term = False

    with _open(path) as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if line == "[Term]":
                # flush the previous term before starting a new one
                if cur and cur["id"]:
                    terms[cur["id"]] = cur
                # parents kept as an ordered list (OBO file order) — the tool's
                # tree arrangement depends on is_a edge order, not id sorting.
                cur = {"id": None, "name": None, "namespace": None,
                       "parents": [], "obsolete": False, "alts": []}
                in_term = True
                continue
            if line.startswith("[") and line.endswith("]"):
                # entering a non-Term stanza (e.g. [Typedef]); flush current term
                if in_term and cur and cur["id"]:
                    terms[cur["id"]] = cur
                in_term = False
                cur = None
                continue
            if not in_term or cur is None or not line:
                continue

            key, _, value = line.partition(": ")
            value = value.strip()
            if key == "id":
                cur["id"] = value
            elif key == "name":
                cur["name"] = value
            elif key == "namespace":
                cur["namespace"] = value
            elif key == "alt_id":
                cur["alts"].append(value)
            elif key == "is_a":
                parent = value.split(" ! ")[0].strip()
                if parent not in cur["parents"]:
                    cur["parents"].append(parent)
            elif key == "relationship":
                rel, _, target = value.partition(" ")
                if rel == "part_of":
                    parent = target.split(" ! ")[0].strip()
                    if parent not in cur["parents"]:
                        cur["parents"].append(parent)
            elif key == "is_obsolete" and value == "true":
                cur["obsolete"] = True

    if in_term and cur and cur["id"]:
        terms[cur["id"]] = cur

    # Drop obsolete terms; register alt_ids -> primary.
    primary = {tid: t for tid, t in terms.items() if not t["obsolete"]}
    for tid, t in primary.items():
        for alt in t["alts"]:
            alt_ids[alt] = tid
    return primary, alt_ids


def _ancestors(term: str, terms: dict[str, dict], cache: dict[str, set]) -> set[str]:
    """All ancestors of a term (inclusive) via direct parents, memoized."""
    if term in cache:
        return cache[term]
    result = {term}
    for parent in terms.get(term, {}).get("parents", ()):  # parent must be a known term
        if parent in terms:
            result |= _ancestors(parent, terms, cache)
    cache[term] = result
    return result


def parse_gaf(path: Path | str) -> dict[str, set[str]]:
    """Parse a GAF v2.0 file into {gene_id: set(direct GO term_id)}.

    Skips comments and NOT-qualified annotations. Column 2 = DB Object ID (gene),
    column 4 = qualifier, column 5 = GO ID.
    """
    gene_terms: dict[str, set[str]] = defaultdict(set)
    with _open(path) as handle:
        for line in handle:
            if line.startswith("!") or not line.strip():
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 5:
                continue
            qualifier, gene, go_id = cols[3], cols[1], cols[4]
            if "NOT" in qualifier.split("|"):
                continue
            if gene and go_id.startswith("GO:"):
                gene_terms[gene].add(go_id)
    return gene_terms


def build_index(obo_path: Path | str, gaf_path: Path | str) -> dict[str, Any]:
    """Build the enrichment index from an OBO + GAF."""
    terms, alt_ids = parse_obo(obo_path)
    direct = parse_gaf(gaf_path)

    anc_cache: dict[str, set] = {}
    gene_terms: dict[str, list] = {}
    term_genes: dict[str, set] = defaultdict(set)

    for gene, go_ids in direct.items():
        propagated: set[str] = set()
        for go_id in go_ids:
            go_id = alt_ids.get(go_id, go_id)
            if go_id not in terms:
                continue
            propagated |= _ancestors(go_id, terms, anc_cache)
        if not propagated:
            continue
        gene_terms[gene] = sorted(propagated)
        for term in propagated:
            term_genes[term].add(gene)

    term_total = {t: len(genes) for t, genes in term_genes.items()}
    term_meta = {
        t: {
            "name": terms[t]["name"],
            "aspect": NAMESPACE_TO_ASPECT.get(terms[t]["namespace"]),
            "parents": [p for p in terms[t]["parents"] if p in terms],
        }
        for t in term_genes
    }

    return {
        "gene_terms": gene_terms,
        "term_total": term_total,
        "term_genes": {t: sorted(genes) for t, genes in term_genes.items()},
        "term_meta": term_meta,
        "total_genes": len(gene_terms),
    }


def save_index(index: dict[str, Any], path: Path | str) -> None:
    """Persist the index as gzipped JSON."""
    with gzip.open(str(path), "wt") as handle:
        json.dump(index, handle)


def load_index(path: Path | str) -> dict[str, Any]:
    """Load a gzipped-JSON index."""
    with gzip.open(str(path), "rt") as handle:
        return json.load(handle)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--obo", required=True, type=Path)
    parser.add_argument("--gaf", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    idx = build_index(args.obo, args.gaf)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save_index(idx, args.output)
    print(
        f"Wrote {args.output}: {idx['total_genes']} background genes, "
        f"{len(idx['term_total'])} terms."
    )
