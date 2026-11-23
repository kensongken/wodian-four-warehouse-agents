from datetime import UTC, datetime
from importlib import import_module
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from product_engine.contracts.materials import MaterialAssetRef, ProductMaterialsUpdatedV1
from product_engine.security.signatures import sign_request

NOW = 1_786_780_850
SECRET = b"sales-test-secret"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_TENANT_ID = UUID("99999999-9999-4999-8999-999999999999")
PRODUCT_CODE = "SP-00831"


class FakeMaterialPackageService:
    def __init__(self) -> None:
        self.packages = {(TENANT_ID, PRODUCT_CODE): material_package()}

    async def latest(self, tenant_id: UUID, product_code: str):
        return self.packages.get((tenant_id, product_code))


def material_package() -> ProductMaterialsUpdatedV1:
    return ProductMaterialsUpdatedV1(
        schema_version=1,
        event_id=UUID("11111111-1111-4111-8111-111111111111"),
        occurred_at=datetime(2026, 8, 15, 8, 0, tzinfo=UTC),
        tenant_id=TENANT_ID,
        product_code=PRODUCT_CODE,
        package_version=1,
        status="source_ready",
        source_assets=[
            MaterialAssetRef(
                material_id=UUID("44444444-4444-4444-8444-444444444444"),
                upstream_asset_id=UUID("55555555-5555-4555-8555-555555555555"),
                object_key=(
                    "tenants/22222222-2222-4222-8222-222222222222/"
                    "products/SP-00831/source/main.heic"
                ),
                sha256="a" * 64,
                mime_type="image/heic",
                role="main_candidate",
            )
        ],
        generated_assets=[],
        content_status="pending",
        price_status="pending",
    )


def client_and_service() -> tuple[TestClient, FakeMaterialPackageService]:
    routes = import_module("product_engine.api.routes.materials")
    service = FakeMaterialPackageService()
    app = FastAPI()
    app.include_router(routes.build_materials_router(service, SECRET, now=lambda: NOW))
    return TestClient(app), service


def signed_headers(
    product_code: str,
    *,
    tenant_id: UUID = TENANT_ID,
    timestamp: int = NOW,
) -> dict[str, str]:
    path = f"/v1/products/{product_code}/materials/latest"
    signature = sign_request(
        SECRET,
        timestamp,
        "sales",
        tenant_id,
        "GET",
        path,
        b"",
    )
    return {
        "X-Source-System": "sales",
        "X-Tenant-Id": str(tenant_id),
        "X-Signature-Timestamp": str(timestamp),
        "X-Signature": signature,
    }


def get(client: TestClient, product_code: str, **header_options):
    return client.get(
        f"/v1/products/{product_code}/materials/latest",
        headers=signed_headers(product_code, **header_options),
    )


def test_latest_package_exposes_source_assets_and_pending_ai_states() -> None:
    client, _ = client_and_service()

    response = get(client, PRODUCT_CODE)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "source_ready"
    assert len(body["source_assets"]) == 1
    assert body["generated_assets"] == []
    assert body["content_status"] == "pending"
    assert body["price_status"] == "pending"


def test_unknown_product_and_cross_tenant_package_return_404() -> None:
    client, _ = client_and_service()

    unknown = get(client, "SP-NOTFOUND")
    cross_tenant = get(client, PRODUCT_CODE, tenant_id=OTHER_TENANT_ID)

    assert unknown.status_code == 404
    assert unknown.json()["error_code"] == "material_package_not_found"
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["error_code"] == "material_package_not_found"


def test_unsigned_or_stale_material_request_returns_401() -> None:
    client, _ = client_and_service()

    unsigned = client.get(f"/v1/products/{PRODUCT_CODE}/materials/latest")
    stale = get(client, PRODUCT_CODE, timestamp=NOW - 301)

    assert unsigned.status_code == 401
    assert unsigned.json()["error_code"] == "authentication_failed"
    assert stale.status_code == 401
    assert stale.json()["error_code"] == "authentication_failed"
