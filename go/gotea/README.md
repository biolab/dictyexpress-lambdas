# gotea (vendored, unmodified)

`gotea` is the GO Term Enrichment Analysis engine used by the dictyExpress
`goenrichment` Resolwe process. These C++ sources were extracted verbatim from the
public Genialis image `public.ecr.aws/genialis/resolwebio/rnaseq:6.0.0`
(`/opt/genialis/gotea/`) so the fast Lambda computes **byte-identical** results to
the existing site (validated against the resolwe-bio test fixture and live output
`593852`).

## Note on the p-value
gotea's `hypergeometric()` is the single-point PMF `P(X = matched)`, not the more
standard over-representation upper tail `P(X >= matched)`. We kept it as-is for
exact parity with current dictyExpress. (An upper-tail variant was prototyped and
reverted; if you ever want the more standard statistic, it's a one-line change in
`processor.cpp` `hypergeometric()` — but it changes every p-value vs the live site.)

## Build
`../compile.sh` produces static x86_64 `processor` (+ `preprocessor`) for AWS
Lambda (native g++ on linux/x86_64, otherwise via a docker gcc image).

## Provenance / licensing
Upstream authorship is Genialis. dictyExpress is co-developed by Genialis and the
Bioinformatics Lab (UL FRI). Confirm redistribution terms before publishing this
source in a public repository.
