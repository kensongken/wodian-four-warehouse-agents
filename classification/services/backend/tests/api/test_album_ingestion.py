import json
from importlib import import_module
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from product_engine.domain.errors import EventConflict
from product_engine.security.signatures import sign_request

NOW = 1_786_780_850
SECRET = b"album-test-secret"


class FakeAlbumService:
    def __init__(self) -> None:
        self.payloads: dict[UUID, str] = {}

    async def ingest(self, event, payload_sha256):
        services = import_module("product_engine.services.album_ingestion")
        existing = self.payloads.get(event.event_id)
        if existing is not None and existing != payload_sha256:
            raise EventConflict("event_id_reused_with_different_payload")
        disposition = "duplicate" if existing == payload_sha256 else "accepted"
        self.payloads[event.event_id] = payload_sha256
        return services.IngestionResult(
            event.event_id,
            event.product_code,
            1,
            disposition,
        )


def client_and_service() -> tuple[TestClient, FakeAlbumService]:
    routes = import_module("product_engine.api.routes.album")
    service = FakeAlbumService()
    app = FastAPI()
    app.include_router(routes.build_album_router(service, SECRET, now=lambda: NOW))
    return TestClient(app), service


def encoded(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def signed_headers(
    payload: dict[str, object],
    *,
    timestamp: int = NOW,
    tenant_id: str = "22222222-2222-4222-8222-222222222222",
) -> dict[str, str]:
    body = encoded(payload)
    tenant = UUID(tenant_id)
    signature = sign_request(
        SECRET,
        timestamp,
        "album",
        tenant,
        "POST",
        "/v1/integrations/album/product-image-sets",
        body,
    )
    return {
        "Content-Type": "application/json",
        "X-Source-System": "album",
        "X-Tenant-Id": tenant_id,
        "X-Signature-Timestamp": str(timestamp),
        "X-Signature": signature,
    }


def post(client: TestClient, payload: dict[str, object], **header_options):
    return client.post(
        "/v1/integrations/album/product-image-sets",
        content=encoded(payload),
        headers=signed_headers(payload, **header_options),
    )


def test_album_event_is_accepted_once_and_then_duplicate(valid_album_payload) -> None:
    client, _ = client_and_service()

    first = post(client, valid_album_payload)
    second = post(client, valid_album_payload)

    assert first.status_code == 202
    assert first.json()["disposition"] == "accepted"
    assert second.status_code == 200
    assert second.json()["disposition"] == "duplicate"


def test_album_acknowledgement_returns_reviewable_six_category_candidate(
    valid_album_payload,
) -> None:
    client, _ = client_and_service()

    response = post(client, valid_album_payload)

    assert response.status_code == 202
    decision = response.json()["classification_decision"]
    assert decision["category_code"] == "nanhong"
    assert decision["category_label"] == "南红"
    assert decision["decision_status"] == "candidate"
    assert decision["source"] == "album_import"
    assert decision["review_required"] is True
    assert decision["mall_import_allowed"] is False


def test_album_acknowledgement_falls_back_for_category_outside_first_six(
    valid_album_payload,
) -> None:
    client, _ = client_and_service()
    payload = {**valid_album_payload, "category": "蜜蜡"}

    response = post(client, payload)

    assert response.status_code == 202
    decision = response.json()["classification_decision"]
    assert decision["category_code"] == "information_insufficient"
    assert decision["category_label"] == "信息不足"
    assert decision["decision_status"] == "information_insufficient"
    assert decision["review_required"] is True
    assert decision["mall_import_allowed"] is False


def test_album_rejects_missing_or_stale_signature(valid_album_payload) -> None:
    client, _ = client_and_service()

    missing = client.post(
        "/v1/integrations/album/product-image-sets",
        content=encoded(valid_album_payload),
        headers={"Content-Type": "application/json"},
    )
    stale = post(client, valid_album_payload, timestamp=NOW - 301)

    assert missing.status_code == 401
    assert missing.json()["error_code"] == "authentication_failed"
    assert stale.status_code == 401
    assert stale.json()["error_code"] == "authentication_failed"


def test_album_rejects_tenant_mismatch_and_foreign_object_key(valid_album_payload) -> None:
    client, _ = client_and_service()
    foreign_tenant = post(
        client,
        valid_album_payload,
        tenant_id="99999999-9999-4999-8999-999999999999",
    )
    changed = json.loads(json.dumps(valid_album_payload))
    changed["images"][0]["object_key"] = "tenants/other/products/leak.jpg"
    foreign_object = post(client, changed)

    assert foreign_tenant.status_code == 401
    assert foreign_tenant.json()["error_code"] == "tenant_binding_failed"
    assert foreign_object.status_code == 422
    assert foreign_object.json()["error_code"] == "object_outside_tenant_prefix"


def test_album_rejects_reused_event_id_with_changed_body(valid_album_payload) -> None:
    client, _ = client_and_service()
    assert post(client, valid_album_payload).status_code == 202
    changed = {**valid_album_payload, "category": "翡翠"}

    response = post(client, changed)

    assert response.status_code == 409
    assert response.json()["error_code"] == "event_conflict"
