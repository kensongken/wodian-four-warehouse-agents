from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.contracts.sales import SalesMaterialUsageV1
from product_engine.domain.errors import MaterialUsageReferenceNotFound
from product_engine.models.products import MaterialPackage, MaterialPackageAsset, Product
from product_engine.models.usage import SalesMaterialUsage
from product_engine.services.sales_usage import UsageResult


class UsageRepository:
    """写入销售使用事件，并只接受当前商家商品素材包中的素材编号。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        event: SalesMaterialUsageV1,
        payload_sha256: str,
    ) -> UsageResult:
        del payload_sha256
        package = (
            await self.session.execute(
                select(Product, MaterialPackage)
                .join(MaterialPackage, MaterialPackage.product_id == Product.id)
                .where(
                    Product.tenant_id == event.tenant_id,
                    Product.product_code == event.product_code,
                    MaterialPackage.tenant_id == event.tenant_id,
                    MaterialPackage.version == event.package_version,
                )
                .with_for_update()
            )
        ).one_or_none()
        if package is None:
            raise MaterialUsageReferenceNotFound("material_package_not_found")
        product, material_package = package
        material_ids = set(
            (
                await self.session.execute(
                    select(MaterialPackageAsset.id).where(
                        MaterialPackageAsset.tenant_id == event.tenant_id,
                        MaterialPackageAsset.package_id == material_package.id,
                    )
                )
            ).scalars()
        )
        requested_ids = set(event.material_ids)
        if not requested_ids.issubset(material_ids):
            raise MaterialUsageReferenceNotFound("material_not_in_package")
        self.session.add(
            SalesMaterialUsage(
                id=uuid4(),
                tenant_id=event.tenant_id,
                event_id=event.event_id,
                session_id=event.session_id,
                product_id=product.id,
                package_version=event.package_version,
                usage_type=event.usage_type,
                material_ids=[str(material_id) for material_id in event.material_ids],
                occurred_at=event.occurred_at,
            )
        )
        await self.session.flush()
        return UsageResult("accepted")
