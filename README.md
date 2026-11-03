# dictyExpress Lambdas

Small AWS Lambda services that back dictyExpress, each exposed as a same-origin
route on the app's CloudFront distribution (e.g. `/find-similar`, `/go`). They
replace slow interactive Resolwe processes with direct endpoints over
precomputed / cached data.

```
browser ─POST /<route>─▶ CloudFront (app.dictyexpress.org)
                              ▼
                        API Gateway (HTTP API)   ── one per service
                              ▼
                        Lambda (handler.py)  ──reads──▶ S3 / Resolwe REST API
```

## Layout

```
common/            shared, bundled into every lambda zip
  http.py          API Gateway + CORS response/parse helpers
  cache.py         LruCache — byte-bounded, thread-safe in-memory cache
  resolwe.py       minimal public Resolwe REST read client
find-similar/      gene similarity over precomputed expression matrices
  handler.py core.py build_matrix.py build_resolwe.py local_server.py
  matrices/        local .npz cache (gitignored; lives in S3 in prod)
go/                GO enrichment (stub — fill in compute_enrichment)
cell-de/           differential expression of user-selected single cells vs a reference
  handler.py core.py build_cellwise.py local_server.py test_core.py
  matrices/        local <strain>.npz cell-wise counts (gitignored; S3 in prod)
aws/               deploy tooling, parameterized by service
  common.env       shared AWS config (region, account, CloudFront id)
  lib.sh build.sh deploy.sh cloudfront_attach.sh teardown.sh upload_matrices.sh
```

Each service has a `service.env` (function name, route, memory, whether it needs
an S3 bucket, Lambda env vars).

## Deploy a service

```bash
cd aws
./deploy.sh find-similar              # S3 (if needed) + IAM + Lambda + API Gateway
./upload_matrices.sh find-similar     # optional: pre-seed matrices into S3
./cloudfront_attach.sh find-similar <api-id>   # add /find-similar to the app CDN
```

`deploy.sh` is idempotent — re-run it to push code/config changes. To change a
lambda's code: edit its files (and/or `common/`), then `./deploy.sh <service>`.

Prereqs: AWS CLI v2 (configured), Python 3 with pip (for the Linux numpy wheel).
No Docker.

## Add a new lambda
1. `cp -r go new-service` (or start fresh) and edit `handler.py` to use
   `common.http` / `common.resolwe` / `common.cache`.
2. Set its `service.env` (`FUNCTION_NAME`, `ROUTE`, `NEEDS_S3`, `LAMBDA_ENV`).
3. `cd aws && ./deploy.sh new-service && ./cloudfront_attach.sh new-service <api-id>`.

## Continuous deployment (GitHub Actions)

Pushes to `main` auto-deploy via `.github/workflows/deploy.yml`, using **GitHub
OIDC** — no AWS keys are stored in GitHub. Each service whose folder changed is
tested (`pytest`, if it has tests) and then deployed; changes to `common/` or
`aws/` deploy all of them. The workflow only updates function code+config
(`aws/update_code.sh`); infra is provisioned once with `deploy.sh`, and data
(e.g. cell-de's strain files) is uploaded with `upload_matrices.sh`. A new
service goes in the workflow's `deployable` list and `setup_github_oidc.sh`.

One-time connection (run locally with admin AWS creds):

```bash
cd aws
./deploy.sh find-similar            # provision infra once (if not already done)
./setup_github_oidc.sh              # creates the OIDC provider + scoped IAM role
```

Then set the printed role ARN as a GitHub **repository variable**
`AWS_DEPLOY_ROLE_ARN` (Settings > Secrets and variables > Actions > Variables).
The role is scoped to this repo's `main` branch and may only update code on the
named lambda functions. You can also trigger a deploy manually from the Actions
tab (workflow_dispatch).

## Local development (find-similar)

```bash
cd find-similar
python3 local_server.py --cache-mb 64     # POST http://127.0.0.1:8787/find-similar
```

## Tear down

```bash
cd aws
./teardown.sh find-similar                # removes API Gateway + Lambda + role; keeps S3
./teardown.sh find-similar --delete-bucket
```
Removing a service's CloudFront route/origin is manual (shared distribution).

## Local development (cell-de)

`cell-de` tests the cells a user selects on the single-cell UMAP against a
reference (default: all other cells of the strain) for every gene. Its input is
a cell-wise copy of the app's single-cell zarr data
(`data_prep/prepare_single_cell_zarr.py` output), built once per strain:

```bash
cd cell-de
python build_cellwise.py --input <data>/single_cell/AX4   # -> matrices/AX4.npz (~220 MB)
python local_server.py                                     # POST http://127.0.0.1:8788/cell-de
python -m pytest test_core.py
```

The cell-wise data includes each cell's biological replicate when the strain
folder has `cells/replicate.json` (written by `data_prep/add_replicates.py` from
the h5ad's `obs["marker"]`: the separately grown fluorescent lines of AX4 and
acaA-, the r1-r3 libraries of acaA-pkaC). Rebuild with `build_cellwise.py` after
adding it.

Request `{"strain": "AX4", "cells": [...], "reference": [...]}` (`reference`
optional). The response is columnar, one list per field aligned with `genes`:

- `log2_fold_change`, `cohens_d`: the marker ranking (log1p of counts scaled to
  10k per cell; d with the SD averaged over both groups, as in scran).
- `replicate_pvalue`, `replicate_fdr`, `replicate_log2_fold_change`: pseudobulk
  test across replicates (limma paired design on voom log-CPM with empirical
  Bayes, matching limma to ~1e-14); `null` for genes too lowly expressed to test.
  `replicate_test` says whether it ran (it needs 2 replicates with 10 selected
  and 10 reference cells each) and which replicates it used.
- `t`, `pvalue`, `neg_log10_pvalue`, `fdr`, `neg_log10_fdr`: scanpy's
  `t-test_overestim_var` over cells. Cells aren't independent replicates, so
  these are far too small (often < 1e-1000); kept only for reference.

The cells are chosen by looking at the same data, so even the replicate
p-values are exploratory.

Deploy: `cd aws && ./deploy.sh cell-de && ./upload_matrices.sh cell-de`, then
`./cloudfront_attach.sh cell-de <api-id>`.
