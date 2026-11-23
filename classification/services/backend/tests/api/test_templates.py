import json
from importlib import import_module
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from product_engine.security.signatures import sign_request
from tests.fakes.object_storage import FakeObjectStorage

NOW = 1_786_780_850
SECRET = b"admin-test-secret"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_TENANT_ID = UUID("99999999-9999-4999-8999-999999999999")
OBJECT_KEY = (
    "tenants/22222222-2222-4222-8222-222222222222/templates/NANHONG-BRACELET-MAIN/v1/source.jpg"
)


class FakeTemplateRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[UUID, str, int], object] = {}

    async def exists_version(self, tenant_id, template_code, version):
        return (tenant_id, template_code, version) in self.items

    async def create(self, record):
        self.items[(record.tenant_id, record.template_code, record.version)] = record
        return record

    async def list_active(self, tenant_id, category, status):
        return [
            item
            for (item_tenant, _, _), item in self.items.items()
            if item_tenant == tenant_id and item.category == category and item.status == status
        ]


def make_client(
    storage: FakeObjectStorage,
    repository: FakeTemplateRepository,
    tenant_id: UUID = TENANT_ID,
) -> TestClient:
    routes = import_module("product_engine.api.routes.templates")
    registry_module = import_module("product_engine.services.template_registry")
    registry = registry_module.TemplateRegistry(storage, repository)
    app = FastAPI()
    app.include_router(routes.build_templates_router(registry, SECRET, now=lambda: NOW))
    client = TestClient(app)
    client.headers.update({"X-Test-Tenant": str(tenant_id)})
    return client


@pytest.fixture
def fake_storage() -> FakeObjectStorage:
    return FakeObjectStorage()


@pytest.fixture
def repository() -> FakeTemplateRepository:
    return FakeTemplateRepository()


@pytest.fixture
def admin_client(fake_storage, repository) -> TestClient:
    return make_client(fake_storage, repository)


@pytest.fixture
def other_admin_client(fake_storage, repository) -> TestClient:
    return make_client(fake_storage, repository, OTHER_TENANT_ID)


@pytest.fixture
def prepared_template_request() -> dict[str, object]:
    return {
        "template_code": "NANHONG-BRACELET-MAIN",
        "version": 1,
        "category": "南红",
        "image_type": "main",
        "object_key": OBJECT_KEY,
        "mime_type": "image/jpeg",
        "byte_size": 820_000,
        "sha256": "b" * 64,
    }


def signed_request(
    client: TestClient,
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
):
    body = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if payload is not None
        else b""
    )
    tenant_id = UUID(client.headers["X-Test-Tenant"])
    signing_path = path.split("?", 1)[0]
    signature = sign_request(
        SECRET,
        NOW,
        "admin",
        tenant_id,
        method,
        signing_path,
        body,
    )
    headers = {
        "X-Source-System": "admin",
        "X-Tenant-Id": str(tenant_id),
        "X-Signature-Timestamp": str(NOW),
        "X-Signature": signature,
    }
    return client.request(method, path, content=body, headers=headers)


def test_template_is_activated_only_after_object_validation(admin_client, fake_storage) -> None:
    upload_payload = {
        "template_code": "NANHONG-BRACELET-MAIN",
        "version": 1,
        "category": "南红",
        "image_type": "main",
        "mime_type": "image/jpeg",
        "byte_size": 820_000,
        "sha256": "b" * 64,
    }
    upload = signed_request(admin_client, "POST", "/v1/templates/uploads", payload=upload_payload)
    assert upload.status_code == 201
    fake_storage.complete(upload.json()["object_key"], "image/jpeg", 820_000, "b" * 64)

    finalized = signed_request(
        admin_client,
        "POST",
        "/v1/templates",
        payload={**upload_payload, "object_key": upload.json()["object_key"]},
    )
    assert finalized.status_code == 201
    assert finalized.json()["status"] == "active"


@pytest.mark.parametrize(
    ("stored_mime", "stored_size", "stored_sha"),
    [
        ("image/png", 820_000, "b" * 64),
        ("image/jpeg", 20_971_521, "b" * 64),
        ("image/jpeg", 820_000, "c" * 64),
    ],
)
def test_template_rejects_mismatched_object(
    admin_client,
    prepared_template_request,
    fake_storage,
    stored_mime,
    stored_size,
    stored_sha,
) -> None:
    fake_storage.complete(
        prepared_template_request["object_key"], stored_mime, stored_size, stored_sha
    )
    response = signed_request(
        admin_client, "POST", "/v1/templates", payload=prepared_template_request
    )
    assert response.status_code == 422


def test_template_rejects_missing_object(admin_client, prepared_template_request) -> None:
    response = signed_request(
        admin_client, "POST", "/v1/templates", payload=prepared_template_request
    )
    assert response.status_code == 422


def test_template_version_is_immutable(
    admin_client, prepared_template_request, fake_storage
) -> None:
    fake_storage.complete(OBJECT_KEY, "image/jpeg", 820_000, "b" * 64)
    first = signed_request(admin_client, "POST", "/v1/templates", payload=prepared_template_request)
    second = signed_request(
        admin_client, "POST", "/v1/templates", payload=prepared_template_request
    )
    assert first.status_code == 201
    assert second.status_code == 409


def test_template_list_is_tenant_scoped(
    admin_client,
    other_admin_client,
    prepared_template_request,
    fake_storage,
) -> None:
    fake_storage.complete(OBJECT_KEY, "image/jpeg", 820_000, "b" * 64)
    signed_request(admin_client, "POST", "/v1/templates", payload=prepared_template_request)
    response = signed_request(
        other_admin_client,
        "GET",
        "/v1/templates?category=南红&status=active",
        payload=None,
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_template_rejects_foreign_object_key(
    other_admin_client, prepared_template_request, fake_storage
) -> None:
    fake_storage.complete(OBJECT_KEY, "image/jpeg", 820_000, "b" * 64)
    response = signed_request(
        other_admin_client,
        "POST",
        "/v1/templates",
        payload=prepared_template_request,
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "object_outside_tenant_prefix"


def test_template_routes_require_admin_signature(admin_client) -> None:
    response = admin_client.get("/v1/templates?category=南红&status=active")

    assert response.status_code == 401
    assert response.json()["error_code"] == "authentication_failed"
    assert "管理员签名" in response.json()["message_zh"]


def test_template_list_returns_stable_active_record(
    admin_client,
    prepared_template_request,
    fake_storage,
) -> None:
    fake_storage.complete(OBJECT_KEY, "image/jpeg", 820_000, "b" * 64)
    signed_request(
        admin_client,
        "POST",
        "/v1/templates",
        payload=prepared_template_request,
    )

    response = signed_request(
        admin_client,
        "GET",
        "/v1/templates?category=南红&status=active",
    )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "id": response.json()["items"][0]["id"],
            "tenant_id": str(TENANT_ID),
            **prepared_template_request,
            "status": "active",
        }
    ]
