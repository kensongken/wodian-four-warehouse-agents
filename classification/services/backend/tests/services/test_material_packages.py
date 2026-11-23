from datetime import UTC, datetime
from uuid import UUID

import pytest

from product_engine.contracts.materials import MaterialAssetRef, ProductMaterialsUpdatedV1

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
PRODUCT_CODE = "SP-00831"


class FakeProductRepository:
    def __init__(self, package: ProductMaterialsUpdatedV1 | None) -> None:
        self.package = package
        self.requests: list[tuple[UUID, str]] = []

    async def latest_material_package(self, tenant_id: UUID, product_code: str):
        self.requests.append((tenant_id, product_code))
        return self.package


def package() -> ProductMaterialsUpdatedV1:
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
                object_key="tenants/22222222-2222-4222-8222-222222222222/products/SP-00831/source/main.heic",
                sha256="a" * 64,
                mime_type="image/heic",
                role="main_candidate",
            )
        ],
        generated_assets=[],
        content_status="pending",
        price_status="pending",
    )


@pytest.mark.asyncio
async def test_latest_returns_the_tenant_scoped_source_only_package() -> None:
    from product_engine.services.material_packages import MaterialPackageService

    repository = FakeProductRepository(package())

    result = await MaterialPackageService(repository).latest(TENANT_ID, PRODUCT_CODE)

    assert result is not None
    assert result.status == "source_ready"
    assert result.generated_assets == []
    assert result.content_status == "pending"
    assert result.price_status == "pending"
    assert repository.requests == [(TENANT_ID, PRODUCT_CODE)]


@pytest.mark.asyncio
async def test_latest_returns_none_when_the_tenant_has_no_package() -> None:
    from product_engine.services.material_packages import MaterialPackageService

    result = await MaterialPackageService(FakeProductRepository(None)).latest(
        TENANT_ID, PRODUCT_CODE
    )

    assert result is None
