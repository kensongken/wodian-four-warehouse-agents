from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.contracts.materials import MaterialAssetRef, ProductMaterialsUpdatedV1
from product_engine.domain.errors import EventConflict
from product_engine.models.products import (
    MaterialPackage,
    MaterialPackageAsset,
    Product,
    SourceImage,
)


def product_lookup_statement(tenant_id: UUID, product_code: str) -> Select[tuple[Product]]:
    return (
        select(Product)
        .where(
            Product.tenant_id == tenant_id,
            Product.product_code == product_code,
        )
        .with_for_update()
    )


def latest_package_statement(tenant_id: UUID, product_code: str) -> Select[tuple[int]]:
    return (
        select(func.max(MaterialPackage.version))
        .join(Product, Product.id == MaterialPackage.product_id)
        .where(
            MaterialPackage.tenant_id == tenant_id,
            Product.tenant_id == tenant_id,
            Product.product_code == product_code,
        )
    )


def material_package_by_event_statement(
    tenant_id: UUID,
    package_event_id: UUID,
    product_code: str,
) -> Select[tuple[Product, MaterialPackage]]:
    return (
        select(Product, MaterialPackage)
        .join(MaterialPackage, MaterialPackage.product_id == Product.id)
        .where(
            Product.tenant_id == tenant_id,
            Product.product_code == product_code,
            MaterialPackage.tenant_id == tenant_id,
            MaterialPackage.published_event_id == package_event_id,
        )
    )


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def exists(self, tenant_id: UUID, product_code: str) -> bool:
        return (
            await self.session.execute(
                select(Product.id).where(
                    Product.tenant_id == tenant_id,
                    Product.product_code == product_code,
                )
            )
        ).scalar_one_or_none() is not None

    async def latest_package_version(self, tenant_id: UUID, product_code: str) -> int:
        version = (
            await self.session.execute(latest_package_statement(tenant_id, product_code))
        ).scalar_one_or_none()
        if version is None:
            raise EventConflict("material_package_not_found")
        return version

    async def latest_material_package(
        self,
        tenant_id: UUID,
        product_code: str,
    ) -> ProductMaterialsUpdatedV1 | None:
        row = (
            await self.session.execute(
                select(Product, MaterialPackage)
                .join(MaterialPackage, MaterialPackage.product_id == Product.id)
                .where(
                    Product.tenant_id == tenant_id,
                    Product.product_code == product_code,
                    MaterialPackage.tenant_id == tenant_id,
                )
                .order_by(MaterialPackage.version.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        product, package = row
        asset_rows = (
            await self.session.execute(
                select(MaterialPackageAsset, SourceImage)
                .join(SourceImage, SourceImage.id == MaterialPackageAsset.source_image_id)
                .where(
                    MaterialPackageAsset.tenant_id == tenant_id,
                    MaterialPackageAsset.package_id == package.id,
                    SourceImage.tenant_id == tenant_id,
                )
                .order_by(MaterialPackageAsset.position)
            )
        ).all()
        return ProductMaterialsUpdatedV1(
            schema_version=1,
            event_id=package.published_event_id,
            occurred_at=package.created_at,
            tenant_id=tenant_id,
            product_code=product.product_code,
            package_version=package.version,
            status="source_ready",
            source_assets=[
                MaterialAssetRef(
                    material_id=membership.id,
                    upstream_asset_id=image.upstream_asset_id,
                    object_key=image.object_key,
                    sha256=image.sha256,
                    mime_type=image.mime_type,
                    role=image.role,
                )
                for membership, image in asset_rows
            ],
            generated_assets=[],
            content_status="pending",
            price_status="pending",
        )

    async def upsert_image_set(
        self,
        event: ProductImageSetReadyV1,
        *,
        package_event_id: UUID | None = None,
    ) -> int:
        product = (
            await self.session.execute(
                product_lookup_statement(event.tenant_id, event.product_code)
            )
        ).scalar_one_or_none()
        changed = product is None
        if product is None:
            product = Product(
                id=uuid4(),
                tenant_id=event.tenant_id,
                product_code=event.product_code,
                source_task_id=event.source_task_id,
                category=event.category,
                form=event.form,
                classification_confidence=Decimal(str(event.classification_confidence)),
            )
            self.session.add(product)
        else:
            incoming = (
                event.source_task_id,
                event.category,
                event.form,
                Decimal(str(event.classification_confidence)),
            )
            current = (
                product.source_task_id,
                product.category,
                product.form,
                product.classification_confidence,
            )
            if incoming != current:
                changed = True
                (
                    product.source_task_id,
                    product.category,
                    product.form,
                    product.classification_confidence,
                ) = incoming

        existing_images = (
            await self.session.execute(
                select(SourceImage).where(
                    SourceImage.tenant_id == event.tenant_id,
                    SourceImage.product_id == product.id,
                )
            )
        ).scalars()
        images_by_upstream = {image.upstream_asset_id: image for image in existing_images}
        ordered_images: list[SourceImage] = []
        for source in event.images:
            image = images_by_upstream.get(source.asset_id)
            incoming_metadata = (
                source.object_key,
                source.sha256,
                source.mime_type,
                source.kind,
                source.role,
                Decimal(str(source.quality_confidence)),
            )
            if image is None:
                changed = True
                image = SourceImage(
                    id=uuid4(),
                    tenant_id=event.tenant_id,
                    product_id=product.id,
                    upstream_asset_id=source.asset_id,
                    object_key=source.object_key,
                    sha256=source.sha256,
                    mime_type=source.mime_type,
                    kind=source.kind,
                    role=source.role,
                    quality_confidence=Decimal(str(source.quality_confidence)),
                )
                self.session.add(image)
            else:
                current_metadata = (
                    image.object_key,
                    image.sha256,
                    image.mime_type,
                    image.kind,
                    image.role,
                    image.quality_confidence,
                )
                if incoming_metadata != current_metadata:
                    changed = True
                    (
                        image.object_key,
                        image.sha256,
                        image.mime_type,
                        image.kind,
                        image.role,
                        image.quality_confidence,
                    ) = incoming_metadata
            ordered_images.append(image)

        latest_version = (
            await self.session.execute(
                latest_package_statement(event.tenant_id, event.product_code)
            )
        ).scalar_one_or_none()
        if package_event_id is not None:
            changed = True
        if not changed and latest_version is not None:
            return latest_version

        version = (latest_version or 0) + 1
        package = MaterialPackage(
            id=uuid4(),
            tenant_id=event.tenant_id,
            product_id=product.id,
            version=version,
            status="source_ready",
            content_status="pending",
            price_status="pending",
            published_event_id=package_event_id or uuid4(),
            created_at=datetime.now(UTC),
        )
        self.session.add(package)
        for position, image in enumerate(ordered_images):
            self.session.add(
                MaterialPackageAsset(
                    id=uuid4(),
                    tenant_id=event.tenant_id,
                    package_id=package.id,
                    source_image_id=image.id,
                    position=position,
                )
            )
        await self.session.flush()
        return version

    async def required_material_package_version_by_event(
        self,
        tenant_id: UUID,
        package_event_id: UUID,
        product_code: str,
        expected_event: ProductImageSetReadyV1 | None = None,
    ) -> int:
        row = (
            await self.session.execute(
                material_package_by_event_statement(
                    tenant_id,
                    package_event_id,
                    product_code,
                )
            )
        ).one_or_none()
        if row is None:
            raise EventConflict("material_package_event_not_found")
        _, package = row
        if expected_event is not None:
            stored_assets = (
                await self.session.execute(
                    select(MaterialPackageAsset, SourceImage)
                    .join(SourceImage, SourceImage.id == MaterialPackageAsset.source_image_id)
                    .where(
                        MaterialPackageAsset.tenant_id == tenant_id,
                        MaterialPackageAsset.package_id == package.id,
                        SourceImage.tenant_id == tenant_id,
                    )
                    .order_by(MaterialPackageAsset.position)
                )
            ).all()
            stored_identity = [
                (
                    image.upstream_asset_id,
                    image.object_key,
                    image.sha256,
                    image.mime_type,
                    image.kind,
                    image.role,
                )
                for _, image in stored_assets
            ]
            expected_identity = [
                (
                    image.asset_id,
                    image.object_key,
                    image.sha256,
                    image.mime_type,
                    image.kind,
                    image.role,
                )
                for image in expected_event.images
            ]
            if stored_identity != expected_identity:
                raise EventConflict("material_package_event_asset_mismatch")
        return cast(int, package.version)

    async def build_materials_event(
        self,
        tenant_id: UUID,
        product_code: str,
        version: int,
    ) -> dict[str, object]:
        row = (
            await self.session.execute(
                select(Product, MaterialPackage)
                .join(MaterialPackage, MaterialPackage.product_id == Product.id)
                .where(
                    Product.tenant_id == tenant_id,
                    Product.product_code == product_code,
                    MaterialPackage.tenant_id == tenant_id,
                    MaterialPackage.version == version,
                )
            )
        ).one()
        product, package = row
        asset_rows = (
            await self.session.execute(
                select(MaterialPackageAsset, SourceImage)
                .join(SourceImage, SourceImage.id == MaterialPackageAsset.source_image_id)
                .where(
                    MaterialPackageAsset.tenant_id == tenant_id,
                    MaterialPackageAsset.package_id == package.id,
                    SourceImage.tenant_id == tenant_id,
                )
                .order_by(MaterialPackageAsset.position)
            )
        ).all()
        source_assets = [
            MaterialAssetRef(
                material_id=membership.id,
                upstream_asset_id=image.upstream_asset_id,
                object_key=image.object_key,
                sha256=image.sha256,
                mime_type=image.mime_type,
                role=image.role,
            )
            for membership, image in asset_rows
        ]
        event = ProductMaterialsUpdatedV1(
            schema_version=1,
            event_id=package.published_event_id,
            occurred_at=package.created_at,
            tenant_id=tenant_id,
            product_code=product.product_code,
            package_version=package.version,
            status="source_ready",
            source_assets=source_assets,
            generated_assets=[],
            content_status="pending",
            price_status="pending",
        )
        return cast(dict[str, object], event.model_dump(mode="json"))
