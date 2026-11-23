from typing import Protocol
from uuid import UUID

from product_engine.contracts.materials import ProductMaterialsUpdatedV1


class MaterialPackageReader(Protocol):
    async def latest_material_package(
        self,
        tenant_id: UUID,
        product_code: str,
    ) -> ProductMaterialsUpdatedV1 | None: ...


class MaterialPackageService:
    def __init__(self, packages: MaterialPackageReader) -> None:
        self.packages = packages

    async def latest(
        self,
        tenant_id: UUID,
        product_code: str,
    ) -> ProductMaterialsUpdatedV1 | None:
        return await self.packages.latest_material_package(tenant_id, product_code)
