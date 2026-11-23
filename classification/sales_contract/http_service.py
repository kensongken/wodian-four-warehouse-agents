"""Dependency-free WSGI contract for synthetic sales product lookups."""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, Iterable, Tuple

from .catalog import CODE_TYPES, SalesCatalog
from scripts.validate_product_passports import strict_json_loads


RESOLUTION_PATH = "/v0.1/sales/product-code-resolution"
PACKAGE_PATH = re.compile(
    r"^/v0\.1/product-packages/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
CONTRACT_VERSION = "0.1"
SALES_AUDIENCE = "sales_journey"
MAX_REQUEST_BYTES = 1_048_576
RESOLUTION_FIELDS = {
    "contract_version",
    "tenant_id",
    "code_type",
    "code",
    "purpose",
}
BUSINESS_STATUS_HTTP = {
    "not_found": 404,
    "ambiguous": 409,
    "retired": 410,
    "not_sellable": 422,
}

StartResponse = Callable[[str, Iterable[Tuple[str, str]]], Callable[[bytes], object]]


def create_sales_product_contract_app(catalog: SalesCatalog):
    """Create a tenant-bound WSGI app over one already-validated catalog."""

    def app(environ: Dict[str, object], start_response: StartResponse):
        method = environ.get("REQUEST_METHOD")
        path = environ.get("PATH_INFO")
        query = environ.get("QUERY_STRING")
        if not isinstance(method, str) or not isinstance(path, str) or not isinstance(query, str):
            return _json_response(start_response, 400, {"error": "invalid_request"})

        if path == RESOLUTION_PATH:
            if method != "POST":
                return _json_response(
                    start_response, 405, {"error": "method_not_allowed"}, allow="POST"
                )
            if query:
                return _json_response(start_response, 400, {"error": "invalid_request"})
            return _resolve_request(environ, start_response, catalog)

        package_match = PACKAGE_PATH.fullmatch(path)
        if package_match:
            if method != "GET":
                return _json_response(
                    start_response, 405, {"error": "method_not_allowed"}, allow="GET"
                )
            return _package_request(
                environ, start_response, catalog, package_match.group(1), query
            )

        return _json_response(start_response, 404, {"error": "not_found"})

    return app


def _resolve_request(
    environ: Dict[str, object], start_response: StartResponse, catalog: SalesCatalog
):
    payload = _read_json_object(environ)
    if payload is None or not _valid_resolution_shape(payload):
        return _json_response(start_response, 400, {"error": "invalid_request"})

    header_tenant_id = environ.get("HTTP_X_TENANT_ID")
    if not isinstance(header_tenant_id, str) or not header_tenant_id:
        return _json_response(start_response, 403, {"error": "forbidden"})
    if header_tenant_id != payload["tenant_id"]:
        return _json_response(start_response, 403, {"error": "forbidden"})

    resolution = catalog.resolve(
        payload["tenant_id"], payload["code_type"], payload["code"]
    )
    if resolution.get("resolution_status") == "resolved":
        return _json_response(start_response, 200, resolution)
    if resolution.get("resolution_status") == "tenant_mismatch":
        return _json_response(start_response, 403, {"error": "forbidden"})
    resolution_status = resolution.get("resolution_status")
    http_status = BUSINESS_STATUS_HTTP.get(resolution_status, 404)
    if resolution_status not in BUSINESS_STATUS_HTTP:
        resolution_status = "not_found"
    return _json_response(
        start_response,
        http_status,
        {
            "contract_version": CONTRACT_VERSION,
            "resolution_status": resolution_status,
        },
    )


def _package_request(
    environ: Dict[str, object], start_response: StartResponse, catalog: SalesCatalog,
    product_id: str, query: str,
):
    header_tenant_id = environ.get("HTTP_X_TENANT_ID")
    if not isinstance(header_tenant_id, str) or not header_tenant_id:
        return _json_response(start_response, 403, {"error": "forbidden"})

    if not _valid_audience_query(query):
        return _json_response(start_response, 400, {"error": "invalid_request"})
    if query != f"audience={SALES_AUDIENCE}":
        return _json_response(start_response, 403, {"error": "forbidden"})

    package = catalog.sales_package(header_tenant_id, product_id)
    if package.get("audience") == SALES_AUDIENCE:
        return _json_response(start_response, 200, package)
    if package.get("status") == "tenant_mismatch":
        return _json_response(start_response, 403, {"error": "forbidden"})
    package_status = package.get("status")
    http_status = BUSINESS_STATUS_HTTP.get(package_status, 404)
    if package_status not in BUSINESS_STATUS_HTTP:
        package_status = "not_found"
    return _json_response(start_response, http_status, {"status": package_status})


def _valid_resolution_shape(payload: Dict[str, object]) -> bool:
    if set(payload) != RESOLUTION_FIELDS:
        return False
    valid_shape = (
        payload["contract_version"] == CONTRACT_VERSION
        and isinstance(payload["tenant_id"], str)
        and bool(payload["tenant_id"])
        and isinstance(payload["code_type"], str)
        and payload["code_type"] in CODE_TYPES
        and isinstance(payload["code"], str)
        and bool(payload["code"])
        and payload["purpose"] == SALES_AUDIENCE
    )
    if not valid_shape:
        return False
    return not (
        payload["code_type"] == "product_id"
        and not CANONICAL_UUID.fullmatch(payload["code"])
    )


def _valid_audience_query(query: str) -> bool:
    if not query or "&" in query or query.count("=") != 1:
        return False
    key, value = query.split("=", 1)
    return key == "audience" and bool(value)


def _read_json_object(environ: Dict[str, object]):
    content_type = environ.get("CONTENT_TYPE")
    content_length = environ.get("CONTENT_LENGTH")
    body_stream = environ.get("wsgi.input")
    if (
        not isinstance(content_type, str)
        or content_type.lower() != "application/json"
        or not isinstance(content_length, str)
        or not hasattr(body_stream, "read")
    ):
        return None
    try:
        length = int(content_length)
    except ValueError:
        return None
    if length < 1 or length > MAX_REQUEST_BYTES:
        return None
    try:
        raw_body = body_stream.read(length)
        payload = strict_json_loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _json_response(
    start_response: StartResponse,
    status: int,
    payload: Dict[str, object],
    allow: str | None = None,
):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    reasons = {
        200: "OK",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        410: "Gone",
        422: "Unprocessable Content",
    }
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    if allow is not None:
        headers.append(("Allow", allow))
    start_response(f"{status} {reasons[status]}", headers)
    return [body]
