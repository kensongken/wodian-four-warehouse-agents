import json
from importlib import import_module
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from product_engine.domain.errors import EventConflict, MaterialUsageReferenceNotFound
from product_engine.security.signatures import sign_request

NOW = 1_786_780_850
SECRET = b"sales-test-secret"
TENANT_ID = "22222222-2222-4222-8222-222222222222"


class FakeUsageService:
    def __init__(self) -> None:
        self.payloads: dict[UUID, str] = {}

    async def record(self, event, payload_sha256):
        existing = self.payloads.get(event.event_id)
        if existing is not None and existing != payload_sha256:
            raise EventConflict("event_id_reused_with_different_payload")
        disposition = "duplicate" if existing else "accepted"
        self.payloads[event.event_id] = payload_sha256
        return type("Result", (), {"disposition": disposition})()


def payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "11111111-1111-4111-8111-111111111111",
        "occurred_at": "2026-08-15T08:00:00Z",
        "tenant_id": TENANT_ID,
        "session_id": "33333333-3333-4333-8333-333333333333",
        "product_code": "SP-00831",
        "package_version": 1,
        "usage_type": "copied",
        "material_ids": ["44444444-4444-4444-8444-444444444444"],
    }


def client() -> TestClient:
    routes = import_module("product_engine.api.routes.sales")
    app = FastAPI()
    app.include_router(routes.build_sales_router(FakeUsageService(), SECRET, now=lambda: NOW))
    return TestClient(app)


def headers(value: dict[str, object]) -> dict[str, str]:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {
        "Content-Type": "application/json",
        "X-Source-System": "sales",
        "X-Tenant-Id": TENANT_ID,
        "X-Signature-Timestamp": str(NOW),
        "X-Signature": sign_request(
            SECRET,
            NOW,
            "sales",
            UUID(TENANT_ID),
            "POST",
            "/v1/integrations/sales/material-usage",
            body,
        ),
    }


def test_copy_event_is_recorded_once() -> None:
    app = client()
    body = payload()
    first = app.post(
        "/v1/integrations/sales/material-usage",
        content=json.dumps(body, sort_keys=True, separators=(",", ":")),
        headers=headers(body),
    )
    second = app.post(
        "/v1/integrations/sales/material-usage",
        content=json.dumps(body, sort_keys=True, separators=(",", ":")),
        headers=headers(body),
    )
    assert first.status_code == 202
    assert second.status_code == 200


def test_converted_usage_is_rejected_by_contract() -> None:
    invalid = {**payload(), "usage_type": "converted"}
    body = json.dumps(invalid, sort_keys=True, separators=(",", ":"))
    response = client().post(
        "/v1/integrations/sales/material-usage", content=body, headers=headers(invalid)
    )
    assert response.status_code == 422


def test_unknown_material_is_not_accepted() -> None:
    class RejectingUsageService:
        async def record(self, event, payload_sha256):
            raise MaterialUsageReferenceNotFound("material_not_in_package")

    routes = import_module("product_engine.api.routes.sales")
    app = FastAPI()
    app.include_router(routes.build_sales_router(RejectingUsageService(), SECRET, now=lambda: NOW))
    body = payload()
    response = TestClient(app).post(
        "/v1/integrations/sales/material-usage",
        content=json.dumps(body, sort_keys=True, separators=(",", ":")),
        headers=headers(body),
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "material_not_in_package"
