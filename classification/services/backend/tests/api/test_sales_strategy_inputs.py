from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from product_engine.contracts.sales_strategy import (
    SalesStrategyInputV1,
    canonical_sales_strategy_sha256,
)
from product_engine.security.signatures import sign_request

NOW = 1_788_163_200
SECRET = b"sales-test-secret"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_TENANT_ID = UUID("99999999-9999-4999-8999-999999999999")
PRODUCT_CODE = "SYNTHETIC-001"


def strategy(version: int = 1) -> SalesStrategyInputV1:
    value = SalesStrategyInputV1(
        tenant_id=TENANT_ID,
        product_code=PRODUCT_CODE,
        strategy_input_version=version,
        product_data_version=1,
        source_album_event_id="11111111-1111-4111-8111-111111111111",
        source_material_package_version=1,
        source_image_set_sha256="a" * 64,
        images=[
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "sha256": "b" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            }
        ],
        material={"value": "绿松石"},
        form={"value": "手串"},
        unverified_items=["真伪未验证"],
        forbidden_expressions=["保证升值"],
        created_at=datetime(2026, 8, 31, 8, tzinfo=UTC),
        content_sha256="0" * 64,
    )
    return value.model_copy(update={"content_sha256": canonical_sales_strategy_sha256(value)})


class FakeService:
    def __init__(self) -> None:
        self.values = {(TENANT_ID, PRODUCT_CODE, 1): strategy()}

    async def latest(self, tenant_id, product_code):
        values = [
            value
            for (tenant, code, _), value in self.values.items()
            if tenant == tenant_id and code == product_code
        ]
        return max(values, key=lambda item: item.strategy_input_version) if values else None

    async def by_version(self, tenant_id, product_code, version):
        return self.values.get((tenant_id, product_code, version))


class CorruptService(FakeService):
    async def latest(self, tenant_id, product_code):
        return strategy().model_copy(update={"content_sha256": "f" * 64})


def client_and_service() -> tuple[TestClient, FakeService]:
    from product_engine.api.routes.sales_strategy_inputs import (
        build_sales_strategy_inputs_router,
    )

    service = FakeService()
    app = FastAPI()
    app.include_router(build_sales_strategy_inputs_router(service, SECRET, now=lambda: NOW))
    return TestClient(app), service


def headers(product_code: str, *, tenant_id: UUID = TENANT_ID) -> dict[str, str]:
    path = f"/v0.1/sales-strategy-inputs/{product_code}"
    return {
        "X-Source-System": "sales",
        "X-Tenant-Id": str(tenant_id),
        "X-Signature-Timestamp": str(NOW),
        "X-Signature": sign_request(SECRET, NOW, "sales", tenant_id, "GET", path, b""),
    }


def test_signed_sales_reads_latest_and_requested_preview_version() -> None:
    client, _ = client_and_service()
    latest = client.get(
        f"/v0.1/sales-strategy-inputs/{PRODUCT_CODE}", headers=headers(PRODUCT_CODE)
    )
    versioned = client.get(
        f"/v0.1/sales-strategy-inputs/{PRODUCT_CODE}?version=1",
        headers=headers(PRODUCT_CODE),
    )
    assert latest.status_code == 200
    assert versioned.status_code == 200
    assert latest.json()["status"] == "preview_only"
    assert latest.json()["automatic_customer_contact_allowed"] is False
    assert "material_id" not in latest.json()["images"][0]


def test_legacy_strategy_without_material_id_keeps_its_existing_digest() -> None:
    legacy = strategy()

    assert legacy.images[0].material_id is None
    assert canonical_sales_strategy_sha256(legacy) == legacy.content_sha256


def test_unsigned_request_returns_stable_sales_signature_error() -> None:
    client, _ = client_and_service()
    response = client.get(f"/v0.1/sales-strategy-inputs/{PRODUCT_CODE}")
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_sales_signature"


def test_unknown_version_and_cross_tenant_read_return_not_found() -> None:
    client, _ = client_and_service()
    missing = client.get(
        f"/v0.1/sales-strategy-inputs/{PRODUCT_CODE}?version=9",
        headers=headers(PRODUCT_CODE),
    )
    cross_tenant = client.get(
        f"/v0.1/sales-strategy-inputs/{PRODUCT_CODE}",
        headers=headers(PRODUCT_CODE, tenant_id=OTHER_TENANT_ID),
    )
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "sales_strategy_input_not_found"
    assert cross_tenant.status_code == 404


def test_corrupt_content_digest_fails_closed() -> None:
    from product_engine.api.routes.sales_strategy_inputs import (
        build_sales_strategy_inputs_router,
    )

    app = FastAPI()
    app.include_router(
        build_sales_strategy_inputs_router(CorruptService(), SECRET, now=lambda: NOW)
    )
    response = TestClient(app).get(
        f"/v0.1/sales-strategy-inputs/{PRODUCT_CODE}", headers=headers(PRODUCT_CODE)
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "sales_strategy_input_conflict"
