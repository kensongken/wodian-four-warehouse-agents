import json
from importlib import import_module
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from product_engine.security.signatures import sign_request
from product_engine.storage.protocol import PresignedUpload

NOW = 1_786_780_850
SECRET = b"sales-test-secret"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeScreenshotService:
    async def create_upload(self, *, tenant_id, mime_type, byte_size, sha256):
        assert tenant_id == TENANT_ID
        assert (mime_type, byte_size, sha256) == ("image/png", 640_000, "c" * 64)
        return PresignedUpload(
            object_key=f"tenants/{tenant_id}/screenshots/2026/08/test.png",
            url="https://upload.test/screenshot",
            headers={"Content-Type": mime_type, "x-amz-meta-sha256": sha256},
            expires_in_seconds=900,
        )

    async def finalize(self, event, payload_sha256):
        assert event.object_key.startswith(f"tenants/{TENANT_ID}/screenshots/")
        assert len(payload_sha256) == 64
        return type(
            "Result",
            (),
            {"screenshot_import_id": event.screenshot_import_id, "disposition": "accepted"},
        )()


def client() -> TestClient:
    routes = import_module("product_engine.api.routes.screenshots")
    app = FastAPI()
    app.include_router(
        routes.build_screenshots_router(FakeScreenshotService(), SECRET, now=lambda: NOW)
    )
    return TestClient(app)


def headers(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Source-System": "sales",
        "X-Tenant-Id": str(TENANT_ID),
        "X-Signature-Timestamp": str(NOW),
        "X-Signature": sign_request(
            SECRET,
            NOW,
            "sales",
            TENANT_ID,
            "POST",
            "/v1/integrations/sales/screenshots/uploads",
            body,
        ),
    }


def test_screenshot_upload_issues_private_tenant_key() -> None:
    body = json.dumps(
        {"mime_type": "image/png", "byte_size": 640_000, "sha256": "c" * 64},
        separators=(",", ":"),
    ).encode()
    response = client().post(
        "/v1/integrations/sales/screenshots/uploads", content=body, headers=headers(body)
    )
    assert response.status_code == 201
    assert response.json()["object_key"].startswith(f"tenants/{TENANT_ID}/screenshots/")
    assert response.json()["headers"]["x-amz-meta-sha256"] == "c" * 64


def test_completed_screenshot_is_queued() -> None:
    body = json.dumps(
        {
            "schema_version": 1,
            "event_id": "11111111-1111-4111-8111-111111111111",
            "occurred_at": "2026-08-15T08:00:00Z",
            "tenant_id": str(TENANT_ID),
            "screenshot_import_id": "33333333-3333-4333-8333-333333333333",
            "object_key": f"tenants/{TENANT_ID}/screenshots/2026/08/test.png",
            "sha256": "c" * 64,
            "mime_type": "image/png",
            "byte_size": 640_000,
        },
        separators=(",", ":"),
    ).encode()
    response = client().post(
        "/v1/integrations/sales/screenshots/imports", content=body, headers=import_headers(body)
    )
    assert response.status_code == 202
    assert response.json() == {
        "screenshot_import_id": "33333333-3333-4333-8333-333333333333",
        "disposition": "accepted",
    }


def import_headers(body: bytes) -> dict[str, str]:
    request_headers = headers(body)
    request_headers["X-Signature"] = sign_request(
        SECRET,
        NOW,
        "sales",
        TENANT_ID,
        "POST",
        "/v1/integrations/sales/screenshots/imports",
        body,
    )
    return request_headers
