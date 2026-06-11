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
