"""Minimal read client for the dictyExpress Resolwe REST API.

Shared by lambdas that need to read existing Resolwe objects (relations, data,
storage). These are plain REST reads of objects that already exist — no process
queue is involved.

Auth: dictyExpress API reads are public (verified from captured traffic: GET
requests carry no Authorization header and no session cookie). So no credentials
are needed by default. Optional overrides via environment:
  RESOLWE_URL            (default: https://app.dictyexpress.org)
  RESOLWE_AUTH_TOKEN     -> sent as "Authorization: Token <value>"
  RESOLWE_SESSION_COOKIE -> sent as a raw Cookie header (e.g. "sessionid=...")
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ResolweError(RuntimeError):
    """A Resolwe read failed (network, HTTP status, or object not found)."""


def base_url() -> str:
    return os.environ.get("RESOLWE_URL", "https://app.dictyexpress.org").rstrip("/")


def auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = os.environ.get("RESOLWE_AUTH_TOKEN")
    cookie = os.environ.get("RESOLWE_SESSION_COOKIE")
    if token:
        headers["Authorization"] = f"Token {token}"
    if cookie:
        headers["Cookie"] = cookie
    return headers


def get_json(path: str, params: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    """GET an API path and parse the JSON response."""
    url = f"{base_url()}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

    request = urllib.request.Request(url, headers=auth_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as error:
        raise ResolweError(f"Resolwe request failed ({error.code}) for {path}.") from error
    except urllib.error.URLError as error:
        raise ResolweError(f"Resolwe request failed for {path}: {error}") from error


def _as_list(payload: Any) -> list:
    """Normalize a list-or-paginated response into a list of objects."""
    if isinstance(payload, list):
        return payload
    return payload.get("results", []) if isinstance(payload, dict) else []


def get_relation(relation_id: int) -> dict:
    """Fetch one relation by id via the list endpoint with an id filter.

    The detail route (/api/relation/<id>) is not exposed and returns the SPA
    HTML, so the list+filter form is used instead.
    """
    relations = _as_list(get_json("api/relation", {"id": relation_id}))
    if not relations:
        raise ResolweError(f"Relation {relation_id} not found in Resolwe.")
    return relations[0]


def get_data(data_ids: list[int]) -> list[dict]:
    """Fetch Data objects by id."""
    if not data_ids:
        return []
    return _as_list(
        get_json(
            "api/data",
            {"id__in": ",".join(str(i) for i in data_ids), "limit": len(data_ids)},
        )
    )


def get_storage(storage_id: int) -> dict:
    """Fetch a storage object (its `json` field holds the payload)."""
    return get_json(f"api/storage/{storage_id}")
