#!/usr/bin/env bash
# Attach a service's route (e.g. /find-similar, /go) to the existing app
# CloudFront distribution, pointing it at the service's API Gateway origin.
# Idempotent and additive — existing origins and behaviors are left untouched.
#
# Usage:  ./cloudfront_attach.sh <service> <api-gateway-id>
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_service "${1:?usage: ./cloudfront_attach.sh <service> <api-gateway-id>}"
API_ID="${2:?usage: ./cloudfront_attach.sh <service> <api-gateway-id>}"

DIST_ID="${CF_DISTRIBUTION_ID}"
APIGW_DOMAIN="${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"
ROUTE_PATTERN="${ROUTE#/}"          # CloudFront PathPattern has no leading slash
ORIGIN_ID="${FUNCTION_NAME}-apigw"

# AWS-managed policies (same as the existing API Gateway behavior uses).
CACHE_DISABLED="4135ea2d-6df8-44a3-9df3-4b5a84be39ad"           # Managed-CachingDisabled
ORP_ALLVIEWER_NO_HOST="b689b0a8-53d0-40ab-baf2-68738e2966ac"    # Managed-AllViewerExceptHostHeader

echo "Attaching /${ROUTE_PATTERN} on ${DIST_ID} -> ${APIGW_DOMAIN} (origin ${ORIGIN_ID})"
aws cloudfront get-distribution-config --id "${DIST_ID}" > /tmp/cf_dist.json

python3 - "${APIGW_DOMAIN}" "${CACHE_DISABLED}" "${ORP_ALLVIEWER_NO_HOST}" "${ROUTE_PATTERN}" "${ORIGIN_ID}" <<'PY'
import json, sys
domain, cache_id, orp_id, route, origin_id = sys.argv[1:6]
full = json.load(open('/tmp/cf_dist.json'))
etag, cfg = full['ETag'], full['DistributionConfig']
open('/tmp/cf_etag.txt', 'w').write(etag)

origins = cfg['Origins']['Items']
if not any(o['Id'] == origin_id for o in origins):
    origins.append({
        "Id": origin_id, "DomainName": domain, "OriginPath": "",
        "CustomHeaders": {"Quantity": 0},
        "CustomOriginConfig": {
            "HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
            "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
            "OriginReadTimeout": 30, "OriginKeepaliveTimeout": 5,
        },
        "ConnectionAttempts": 3, "ConnectionTimeout": 10,
        "OriginShield": {"Enabled": False}, "OriginAccessControlId": "",
    })
cfg['Origins']['Quantity'] = len(origins)

cfg.setdefault('CacheBehaviors', {'Quantity': 0, 'Items': []})
behaviors = cfg['CacheBehaviors'].setdefault('Items', [])
behaviors = [b for b in behaviors if b['PathPattern'] != route]
behaviors.append({
    "PathPattern": route, "TargetOriginId": origin_id,
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {"Quantity": 7,
        "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
        "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
    "Compress": True, "SmoothStreaming": False, "FieldLevelEncryptionId": "",
    "CachePolicyId": cache_id, "OriginRequestPolicyId": orp_id,
    "LambdaFunctionAssociations": {"Quantity": 0},
    "FunctionAssociations": {"Quantity": 0},
})
cfg['CacheBehaviors']['Items'] = behaviors
cfg['CacheBehaviors']['Quantity'] = len(behaviors)

json.dump(cfg, open('/tmp/cf_dist_new.json', 'w'))
print("origins:", [o['Id'] for o in origins])
print(route, "->", origin_id)
PY

aws cloudfront update-distribution --id "${DIST_ID}" \
  --distribution-config file:///tmp/cf_dist_new.json \
  --if-match "$(cat /tmp/cf_etag.txt)" \
  --query 'Distribution.Status' --output text

echo "Invalidating /${ROUTE_PATTERN}..."
aws cloudfront create-invalidation --distribution-id "${DIST_ID}" \
  --paths "/${ROUTE_PATTERN}" --query 'Invalidation.Status' --output text

echo "Done. CloudFront is deploying (a few minutes)."
