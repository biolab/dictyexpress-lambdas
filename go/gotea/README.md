# gotea (vendored, with one modification)

`gotea` is the GO Term Enrichment Analysis engine used by the dictyExpress
`goenrichment` Resolwe process. These C++ sources were extracted from the public
Genialis image `public.ecr.aws/genialis/resolwebio/rnaseq:6.0.0`
(`/opt/genialis/gotea/`) so the fast Lambda computes **identical** results to the
existing site.

## Our modification
`processor.cpp` `hypergeometric()` was changed from the upstream single-point PMF
`P(X = matched)` to the standard over-representation **upper tail**
`P(X >= matched)` (sum over `[matched .. min(n, K)]`). This is the only change;
the ontology/GAF parsing, annotation propagation, tree/DAG construction, score,
and output format are byte-for-byte upstream behavior (validated against the
resolwe-bio test fixture and live output `593852` for the matched==n case).

## Build
`../compile.sh` produces static x86_64 `processor` (+ `preprocessor`) for AWS
Lambda (native g++ on linux/x86_64, otherwise via a docker gcc image).

## Provenance / licensing
Upstream authorship is Genialis. dictyExpress is co-developed by Genialis and the
Bioinformatics Lab (UL FRI). Confirm redistribution terms before publishing this
source in a public repository.
