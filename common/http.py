"""API Gateway + CloudFront HTTP/CORS helpers shared by every lambda.

Every dictyExpress lambda sits behind the app's CloudFront distribution as a
same-origin route (e.g. /find-similar, /go) and is invoked through an HTTP API
Gateway. These helpers build CORS-correct responses and parse requests for both
API Gateway payload formats (v1 REST / v2 HTTP API).
"""

from __future__ import annotations

import json
from typing import Any


def response(
    status_code: int,
    body: dict[str, Any],
    origin: str | None = None,
    allow_headers: str | None = None,
) -> dict[str, Any]:
    """Return an API Gateway compatible JSON response with CORS headers.

    When a request Origin is present, it is reflected back (instead of "*") and
    Access-Control-Allow-Credentials is set, because browsers reject a wildcard
    origin for credentialed requests (the dev client sends credentials:'include').
    Allowed request headers are reflected from the preflight (the dev client adds
    X-CSRFToken), falling back to the common set.
    """
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": origin or "*",
        "Access-Control-Allow-Headers": allow_headers or "Content-Type,X-CSRFToken",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        # Let the browser cache the preflight so repeat calls skip the OPTIONS
        # round-trip (matters when the GO panel recomputes on gene changes).
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }
    if origin:
        headers["Access-Control-Allow-Credentials"] = "true"
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body),
    }


def request_origin(event: dict[str, Any]) -> str | None:
    """Extract the request Origin header (API Gateway lowercases header keys)."""
    headers = event.get("headers") or {}
    return headers.get("origin") or headers.get("Origin")


def requested_headers(event: dict[str, Any]) -> str | None:
    """Headers the CORS preflight asks to send (Access-Control-Request-Headers)."""
    headers = event.get("headers") or {}
    return headers.get("access-control-request-headers") or headers.get(
        "Access-Control-Request-Headers"
    )


def http_method(event: dict[str, Any]) -> str | None:
    """Request method for both API Gateway payload formats (v1 and v2/HTTP API)."""
    return event.get("httpMethod") or (
        (event.get("requestContext") or {}).get("http") or {}
    ).get("method")


def parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse an API Gateway JSON body (empty for OPTIONS preflight)."""
    if http_method(event) == "OPTIONS":
        return {}

    body = event.get("body", event)
    if isinstance(body, str):
        return json.loads(body or "{}")
    return body or {}
