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


def post_json(path: str, body: dict[str, Any], timeout: int = 30) -> Any:
    """POST a JSON body to an API path and parse the JSON response."""
    url = f"{base_url()}/{path.lstrip('/')}"
    data = json.dumps(body).encode()
    headers = {**auth_headers(), "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as error:
        raise ResolweError(f"Resolwe POST failed ({error.code}) for {path}.") from error
    except urllib.error.URLError as error:
        raise ResolweError(f"Resolwe POST failed for {path}: {error}") from error


def map_gene_ids(
    source_db: str, target_db: str, species: str, gene_ids: list[str]
) -> dict[str, str]:
    """Map gene ids between databases via the public knowledge-base endpoint.

    Returns {source_id: target_id}. Used for GO enrichment when the query genes'
    database differs from the GAF's (e.g. DICTYBASE query -> UniProtKB GAF).
    """
    if not gene_ids:
        return {}
    rows = post_json(
        "api/kb/mapping/search",
        {
            "source_db": source_db,
            "target_db": target_db,
            "source_species": species,
            "target_species": species,
            "source_id__in": ",".join(gene_ids),
        },
    )
    return {r["source_id"]: r["target_id"] for r in rows if "source_id" in r and "target_id" in r}


def list_time_series(community: str = "bcm") -> list[dict]:
    """List all time-series relations for a community."""
    return _as_list(
        get_json("api/relation", {"category": "Time series", "tags": f"community:{community}"})
    )


def expression_data_ids_for_relation(relation: dict) -> list[int]:
    """Resolve a relation's expression Data ids (one per sample/entity).

    The frontend normally supplies these on a request; this lets us build a
    matrix offline (prebuild) or whenever they are not provided. Each entity in
    the relation's partitions has an expression Data object carrying
    `output.exp_json`; if an entity has several, the newest (highest id) wins.
    """
    entities = sorted({int(p["entity"]) for p in relation.get("partitions", [])})
    if not entities:
        return []

    rows = _as_list(
        get_json(
            "api/data",
            {"entity__in": ",".join(map(str, entities)), "limit": max(200, len(entities) * 3)},
        )
    )

    newest_by_entity: dict[int, dict] = {}
    for data in rows:
        if "exp_json" not in (data.get("output") or {}):
            continue
        entity = data.get("entity")
        entity_id = entity.get("id") if isinstance(entity, dict) else entity
        if entity_id is None:
            continue
        entity_id = int(entity_id)
        if entity_id not in newest_by_entity or int(data["id"]) > int(newest_by_entity[entity_id]["id"]):
            newest_by_entity[entity_id] = data

    return [int(newest_by_entity[e]["id"]) for e in sorted(newest_by_entity)]
