"""AWS Lambda handler for dictyExpress GO (Gene Ontology) enrichment.

STUB: wired to the shared common/ helpers so it deploys and answers CORS, but
the enrichment computation is not implemented yet. Fill in `compute_enrichment`.

Expected request (adjust to taste):
    {"genes": ["DDB_G0267178", ...], "organism": "Dictyostelium discoideum"}
"""

from __future__ import annotations

import traceback
from typing import Any

from common.http import (
    http_method,
    parse_body,
    request_origin,
    requested_headers,
    response,
)
from common.resolwe import ResolweError


def compute_enrichment(genes: list[str], organism: str | None) -> dict[str, Any]:
    """Compute GO term enrichment for a gene set.

    TODO: implement. Likely shape:
      - load GO annotations (from S3 via common cache, or fetch from Resolwe),
      - run the enrichment test (e.g. hypergeometric) over the gene set,
      - return sorted enriched terms.
    """
    raise NotImplementedError("GO enrichment is not implemented yet.")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle one GO enrichment request."""
    origin = request_origin(event)
    allow_headers = requested_headers(event)

    if http_method(event) == "OPTIONS":
        return response(200, {}, origin, allow_headers)

    try:
        body = parse_body(event)
        genes = [str(g) for g in body.get("genes", [])]
        organism = body.get("organism")
        if not genes:
            return response(400, {"error": "No genes provided."}, origin, allow_headers)

        result = compute_enrichment(genes, organism)
        return response(200, result, origin, allow_headers)
    except NotImplementedError as error:
        return response(501, {"error": str(error)}, origin, allow_headers)
    except ResolweError as error:
        return response(404, {"error": str(error)}, origin, allow_headers)
    except ValueError as error:
        return response(400, {"error": str(error)}, origin, allow_headers)
    except Exception as error:  # noqa: BLE001
        print("FAIL 500 unexpected:\n" + traceback.format_exc())
        return response(500, {"error": f"{type(error).__name__}: {error}"}, origin, allow_headers)
