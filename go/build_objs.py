#!/usr/bin/env python3
"""Build gotea obo_obj + gaf_obj artifacts for a species and upload to S3.

Offline step (analogous to find-similar's prebuild). For each GO dataset it:
  1. downloads the OBO + GAF files from the Resolwe backend (public signed-URL
     redirect at app.genialis.com/data/<id>/<file>),
  2. runs the gotea `preprocessor` (built by compile.sh) to make obo_obj/gaf_obj,
  3. writes meta.json (the GAF's source db + species, for id mapping),
  4. uploads obo_obj, gaf_obj, meta.json to s3://<bucket>/<prefix>/<species>/.

Datasets are defined in DATASETS below (the dicty one matches the live config:
OBO data 24330, GAF data 490688 source=UniProtKB).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

from build_index import parse_gaf

HERE = os.path.dirname(os.path.abspath(__file__))
RESOLWE = os.environ.get("RESOLWE_FILE_HOST", "https://app.genialis.com")

# Relationship lines gotea's preprocessor follows UP to a parent (see
# gotea/preprocessor.cpp obo_processor). is_a is handled separately; has_part is
# the inverse (the current term becomes a parent of the target).
_PARENT_RELS = ("part_of", "regulates", "positively_regulates", "negatively_regulates")


def _goid(token_tail: str) -> str:
    """First whitespace token of a relationship/is_a value (the GO id)."""
    return token_tail.split()[0] if token_tail.split() else ""


def _obo_parents(obo_path: str) -> tuple[dict[str, set[str]], set[str]]:
    """Parent graph exactly as gotea builds it: parents[term] = is_a + part_of +
    regulates(+/-) targets, plus T for every `T has_part term`. Returns (parents,
    all_term_ids); includes obsolete terms (the preprocessor keeps them)."""
    parents: dict[str, set[str]] = {}
    has_part: list[tuple[str, str]] = []
    term_ids: set[str] = set()
    cur = None
    in_term = False
    with open(obo_path) as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if line == "[Term]":
                in_term, cur = True, None
            elif line.startswith("[") and line.endswith("]"):
                in_term, cur = False, None
            elif in_term and line.startswith("id: "):
                cur = line[4:].strip()
                term_ids.add(cur)
                parents.setdefault(cur, set())
            elif in_term and cur and line.startswith("is_a: "):
                parents[cur].add(_goid(line[6:]))
            elif in_term and cur and line.startswith("relationship: "):
                rel, _, tail = line[len("relationship: ") :].partition(" ")
                if rel in _PARENT_RELS:
                    parents[cur].add(_goid(tail))
                elif rel == "has_part":
                    has_part.append((cur, _goid(tail)))
    for parent, child in has_part:
        parents.setdefault(child, set()).add(parent)
    return parents, term_ids


def prune_obo(obo_path: str, gaf_path: str, out_path: str) -> tuple[int, int]:
    """Write a pruned OBO keeping only terms reachable from the GAF annotations.

    Keep = every directly-annotated GO term plus all its ancestors, where
    "ancestor" follows the exact relationships gotea propagates over. Pruned terms
    have no genes in this GAF, so they can never be enriched nor be an ancestor of
    an enriched term — the result is identical, but the obo_obj the processor
    re-parses on every request is ~8x smaller.
    """
    parents, term_ids = _obo_parents(obo_path)
    direct = parse_gaf(gaf_path)

    # gotea matches GAF terms to primary ids only (no alt_id resolution).
    annotated = {go for go_ids in direct.values() for go in go_ids if go in term_ids}
    keep: set[str] = set()
    stack = list(annotated)
    while stack:
        term = stack.pop()
        if term in keep:
            continue
        keep.add(term)
        stack.extend(p for p in parents.get(term, ()) if p in term_ids and p not in keep)

    # Re-emit the OBO: drop non-kept [Term] stanzas; within kept stanzas, drop
    # is_a/relationship lines whose target isn't kept (e.g. has_part to a pruned
    # child) so the preprocessor's link_terms doesn't fail on a missing term.
    def target_kept(line: str) -> bool:
        if line.startswith("is_a: "):
            return _goid(line[6:]) in keep
        if line.startswith("relationship: "):
            _, _, tail = line[len("relationship: ") :].partition(" ")
            return _goid(tail) in keep
        return True

    with open(obo_path) as handle:
        blocks = handle.read().split("\n\n")
    out_blocks = []
    for block in blocks:
        lines = block.strip("\n").split("\n")
        if lines and lines[0] == "[Term]":
            tid = next((ln[4:].strip() for ln in lines if ln.startswith("id: ")), None)
            if tid not in keep:
                continue
            lines = [ln for ln in lines if target_kept(ln)]
            block = "\n".join(lines)
        out_blocks.append(block.strip("\n"))
    with open(out_path, "w") as handle:
        handle.write("\n\n".join(out_blocks) + "\n")
    return len(keep), len(term_ids)

# species -> {obo (data id, file), gaf (data id, file), gaf_source}
DATASETS = {
    "Dictyostelium discoideum": {
        "obo": (24330, "go.obo.gz"),
        "gaf": (490688, "dicty_go_associations_amigo_2020_10_06.gaf.txt.gz"),
        "gaf_source": "UniProtKB",
    },
}


def _download_gunzip(data_id: int, filename: str, dest: str) -> None:
    url = f"{RESOLWE}/data/{data_id}/{filename}"
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest + ".gz", "wb") as out:
        shutil.copyfileobj(resp, out)
    with gzip.open(dest + ".gz", "rb") as gz, open(dest, "wb") as out:
        shutil.copyfileobj(gz, out)
    os.remove(dest + ".gz")


def build_species(species: str, bucket: str, prefix: str, preprocessor: str) -> None:
    cfg = DATASETS[species]
    slug = species.replace(" ", "_")
    with tempfile.TemporaryDirectory() as tmp:
        obo_txt = os.path.join(tmp, "go.obo")
        gaf_txt = os.path.join(tmp, "annotations.gaf")
        _download_gunzip(*cfg["obo"], dest=obo_txt)
        _download_gunzip(*cfg["gaf"], dest=gaf_txt)

        # Prune the ontology to terms reachable from this GAF (exact, ~5x smaller
        # obo_obj -> the processor re-parses far less on every request).
        pruned_obo = os.path.join(tmp, "go.pruned.obo")
        kept, total = prune_obo(obo_txt, gaf_txt, pruned_obo)
        print(f"  pruned ontology: {kept}/{total} terms")

        obo_obj = os.path.join(tmp, "obo_obj")
        gaf_obj = os.path.join(tmp, "gaf_obj")
        subprocess.run([preprocessor, "obo", pruned_obo, obo_obj], check=True)
        subprocess.run([preprocessor, "gaf", gaf_txt, gaf_obj], check=True)

        meta = os.path.join(tmp, "meta.json")
        with open(meta, "w") as handle:
            json.dump({"gaf_source": cfg["gaf_source"], "species": species}, handle)

        import boto3

        s3 = boto3.client("s3")
        for name, path in [("obo_obj", obo_obj), ("gaf_obj", gaf_obj), ("meta.json", meta)]:
            key = f"{prefix}/{slug}/{name}"
            print(f"  uploading s3://{bucket}/{key} ({os.path.getsize(path)} bytes)")
            s3.upload_file(path, bucket, key)
    print(f"[ok] {species}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", default=os.environ.get("GO_BUCKET"))
    parser.add_argument("--prefix", default=os.environ.get("GO_PREFIX", "go"))
    parser.add_argument("--preprocessor", default=os.path.join(HERE, "bin", "preprocessor"))
    parser.add_argument("--species", action="append", help="default: all in DATASETS")
    args = parser.parse_args()

    if not args.bucket:
        parser.error("--bucket (or GO_BUCKET) is required")
    if not os.path.exists(args.preprocessor):
        parser.error(f"preprocessor not found at {args.preprocessor}; run ./compile.sh first")

    for species in (args.species or list(DATASETS)):
        print(f"building {species}...")
        build_species(species, args.bucket, args.prefix, args.preprocessor)


if __name__ == "__main__":
    main()
