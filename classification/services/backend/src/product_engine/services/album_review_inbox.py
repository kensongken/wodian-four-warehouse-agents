from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.domain.errors import ObjectValidationFailed
from product_engine.models.classifications import ProductClassificationVersion
from product_engine.models.events import OutboxEvent
from product_engine.models.products import (
    MaterialPackage,
    MaterialPackageAsset,
    Product,
    SalesStrategyInputRecord,
    SourceImage,
)
from product_engine.models.sales_handoffs import (
    SalesStrategyInputV2Record,
    WarehouseHandoffRecord,
)
from product_engine.models.style_handoffs import (
    StyleEligibilityInputV2Record,
    StyleWarehouseHandoffRecord,
)


class AlbumReviewInboxFailure(ValueError):
    pass


class AlbumReviewImage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: UUID
    mime_type: str
    role: str


class AlbumReviewProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_code: str
    category: str
    form: str
    package_version: int
    classification_source: Literal["album_candidate", "human_confirmed"] = "album_candidate"
    classification_id: UUID | None = None
    classification_version: int | None = None
    received_at: datetime | None = None
    sales_strategy_status: str | None = None
    sales_strategy_version: int | None = None
    sales_delivery_status: str | None = None
    sales_delivered_at: datetime | None = None
    sales_handoff_id: UUID | None = None
    sales_handoff_status: Literal["prepared", "pending", "accepted", "rejected"] | None = None
    sales_receiver_receipt_id: UUID | None = None
    sales_handoff_rejection_code: str | None = None
    sales_v2_version: int | None = None
    style_handoff_id: UUID | None = None
    style_handoff_status: Literal["prepared", "pending", "accepted", "rejected"] | None = None
    style_decision_receipt_id: UUID | None = None
    style_rejection_code: str | None = None
    style_eligibility_version: int | None = None
    images: tuple[AlbumReviewImage, ...]


@dataclass(frozen=True)
class AlbumReviewImageSource:
    object_key: str
    sha256: str
    mime_type: str


@dataclass(frozen=True)
class AlbumReviewImageContent:
    content: bytes
    mime_type: str


class AlbumReviewStore(Protocol):
    async def list_latest(self, tenant_id: UUID) -> Sequence[AlbumReviewProduct]: ...

    async def image_source(
        self,
        tenant_id: UUID,
        product_code: str,
        package_version: int,
        asset_id: UUID,
    ) -> AlbumReviewImageSource | None: ...


class AlbumObjectReader(Protocol):
    async def read(self, object_key: str) -> bytes: ...


_FORM_LABELS = {
    "unknown": "未知",
    "bangle": "手镯",
    "bead": "单珠",
    "bracelet": "手串",
    "brooch": "胸针",
    "earring": "耳饰",
    "necklace": "项链",
    "pendant": "吊坠",
    "ring": "戒指",
    "strand": "串珠",
    "round_bead_bracelet": "方圆珠手串",
}


def _display_form(value: str) -> str:
    return _FORM_LABELS.get(value, value)


def latest_review_rows_statement(
    tenant_id: UUID,
) -> Select[
    tuple[
        Product,
        MaterialPackage,
        MaterialPackageAsset,
        SourceImage,
        SalesStrategyInputRecord,
        OutboxEvent,
        ProductClassificationVersion,
        SalesStrategyInputV2Record,
        WarehouseHandoffRecord,
        StyleEligibilityInputV2Record,
        StyleWarehouseHandoffRecord,
    ]
]:
    latest_versions = (
        select(
            MaterialPackage.product_id.label("product_id"),
            func.max(MaterialPackage.version).label("version"),
        )
        .where(MaterialPackage.tenant_id == tenant_id)
        .group_by(MaterialPackage.product_id)
        .subquery()
    )
    latest_strategy_versions = (
        select(
            SalesStrategyInputRecord.product_id.label("product_id"),
            func.max(SalesStrategyInputRecord.version).label("version"),
        )
        .where(SalesStrategyInputRecord.tenant_id == tenant_id)
        .group_by(SalesStrategyInputRecord.product_id)
        .subquery()
    )
    latest_classification_versions = (
        select(
            ProductClassificationVersion.tenant_id.label("tenant_id"),
            ProductClassificationVersion.product_id.label("product_id"),
            func.max(ProductClassificationVersion.version).label("version"),
        )
        .where(ProductClassificationVersion.tenant_id == tenant_id)
        .group_by(
            ProductClassificationVersion.tenant_id,
            ProductClassificationVersion.product_id,
        )
        .subquery()
    )
    latest_style_versions = (
        select(
            StyleEligibilityInputV2Record.tenant_id.label("tenant_id"),
            StyleEligibilityInputV2Record.product_id.label("product_id"),
            func.max(StyleEligibilityInputV2Record.version).label("version"),
        )
        .where(StyleEligibilityInputV2Record.tenant_id == tenant_id)
        .group_by(
            StyleEligibilityInputV2Record.tenant_id,
            StyleEligibilityInputV2Record.product_id,
        )
        .subquery()
    )
    ranked_sales_deliveries = (
        select(
            OutboxEvent.id.label("delivery_id"),
            OutboxEvent.aggregate_key.label("aggregate_key"),
            func.row_number()
            .over(
                partition_by=OutboxEvent.aggregate_key,
                order_by=(OutboxEvent.created_at.desc(), OutboxEvent.id.desc()),
            )
            .label("delivery_rank"),
        )
        .where(
            OutboxEvent.tenant_id == tenant_id,
            OutboxEvent.topic == "sales.strategy.input.updated.v1",
        )
        .subquery()
    )
    return (
        select(
            Product,
            MaterialPackage,
            MaterialPackageAsset,
            SourceImage,
            SalesStrategyInputRecord,
            OutboxEvent,
            ProductClassificationVersion,
            SalesStrategyInputV2Record,
            WarehouseHandoffRecord,
            StyleEligibilityInputV2Record,
            StyleWarehouseHandoffRecord,
        )
        .join(MaterialPackage, MaterialPackage.product_id == Product.id)
        .join(
            latest_versions,
            (latest_versions.c.product_id == MaterialPackage.product_id)
            & (latest_versions.c.version == MaterialPackage.version),
        )
        .join(MaterialPackageAsset, MaterialPackageAsset.package_id == MaterialPackage.id)
        .join(SourceImage, SourceImage.id == MaterialPackageAsset.source_image_id)
        .outerjoin(
            latest_strategy_versions,
            latest_strategy_versions.c.product_id == Product.id,
        )
        .outerjoin(
            SalesStrategyInputRecord,
            (SalesStrategyInputRecord.product_id == Product.id)
            & (SalesStrategyInputRecord.version == latest_strategy_versions.c.version)
            & (SalesStrategyInputRecord.tenant_id == tenant_id),
        )
        .outerjoin(
            ranked_sales_deliveries,
            (
                ranked_sales_deliveries.c.aggregate_key
                == literal(f"{tenant_id}:") + Product.product_code
            )
            & (ranked_sales_deliveries.c.delivery_rank == 1),
        )
        .outerjoin(
            OutboxEvent,
            OutboxEvent.id == ranked_sales_deliveries.c.delivery_id,
        )
        .outerjoin(
            latest_classification_versions,
            (latest_classification_versions.c.tenant_id == Product.tenant_id)
            & (latest_classification_versions.c.product_id == Product.id),
        )
        .outerjoin(
            ProductClassificationVersion,
            (ProductClassificationVersion.tenant_id == tenant_id)
            & (ProductClassificationVersion.product_id == Product.id)
            & (ProductClassificationVersion.version == latest_classification_versions.c.version),
        )
        .outerjoin(
            SalesStrategyInputV2Record,
            (SalesStrategyInputV2Record.tenant_id == tenant_id)
            & (SalesStrategyInputV2Record.product_id == Product.id)
            & (
                SalesStrategyInputV2Record.source_classification_id
                == ProductClassificationVersion.id
            ),
        )
        .outerjoin(
            WarehouseHandoffRecord,
            (WarehouseHandoffRecord.tenant_id == tenant_id)
            & (WarehouseHandoffRecord.strategy_input_id == SalesStrategyInputV2Record.id)
            & (WarehouseHandoffRecord.target_warehouse == "sales"),
        )
        .outerjoin(
            latest_style_versions,
            (latest_style_versions.c.tenant_id == Product.tenant_id)
            & (latest_style_versions.c.product_id == Product.id),
        )
        .outerjoin(
            StyleEligibilityInputV2Record,
            (StyleEligibilityInputV2Record.tenant_id == tenant_id)
            & (StyleEligibilityInputV2Record.product_id == Product.id)
            & (StyleEligibilityInputV2Record.version == latest_style_versions.c.version)
            & (
                StyleEligibilityInputV2Record.source_classification_id
                == ProductClassificationVersion.id
            ),
        )
        .outerjoin(
            StyleWarehouseHandoffRecord,
            (StyleWarehouseHandoffRecord.tenant_id == tenant_id)
            & (
                StyleWarehouseHandoffRecord.eligibility_input_id == StyleEligibilityInputV2Record.id
            ),
        )
        .where(
            Product.tenant_id == tenant_id,
            MaterialPackage.tenant_id == tenant_id,
            MaterialPackageAsset.tenant_id == tenant_id,
            SourceImage.tenant_id == tenant_id,
        )
        .order_by(Product.product_code, MaterialPackageAsset.position)
    )


class SqlAlbumReviewStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_latest(self, tenant_id: UUID) -> list[AlbumReviewProduct]:
        rows = (await self.session.execute(latest_review_rows_statement(tenant_id))).all()
        grouped: dict[tuple[UUID, int], list[AlbumReviewImage]] = {}
        products: dict[
            tuple[UUID, int],
            tuple[
                Product,
                MaterialPackage,
                SalesStrategyInputRecord | None,
                OutboxEvent | None,
                ProductClassificationVersion | None,
                SalesStrategyInputV2Record | None,
                WarehouseHandoffRecord | None,
                StyleEligibilityInputV2Record | None,
                StyleWarehouseHandoffRecord | None,
            ],
        ] = {}
        for (
            product,
            package,
            _,
            image,
            strategy,
            delivery,
            classification,
            strategy_v2,
            handoff,
            style_input,
            style_handoff,
        ) in rows:
            key = (product.id, package.version)
            products[key] = (
                product,
                package,
                strategy,
                delivery,
                classification,
                strategy_v2,
                handoff,
                style_input,
                style_handoff,
            )
            grouped.setdefault(key, []).append(
                AlbumReviewImage(
                    asset_id=image.upstream_asset_id,
                    mime_type=image.mime_type,
                    role=image.role,
                )
            )
        return [
            AlbumReviewProduct(
                product_code=product.product_code,
                category=(
                    classification.material_label
                    if classification is not None
                    else product.category
                ),
                form=(
                    _display_form(classification.product_form)
                    if classification is not None
                    else product.form
                ),
                package_version=package.version,
                classification_source=(
                    "human_confirmed" if classification is not None else "album_candidate"
                ),
                classification_id=(classification.id if classification is not None else None),
                classification_version=(
                    classification.version if classification is not None else None
                ),
                received_at=package.created_at,
                sales_strategy_status=(strategy.status if strategy is not None else None),
                sales_strategy_version=(strategy.version if strategy is not None else None),
                sales_delivery_status=(delivery.status if delivery is not None else None),
                sales_delivered_at=(delivery.delivered_at if delivery is not None else None),
                sales_handoff_id=(handoff.id if handoff is not None else None),
                sales_handoff_status=(
                    cast(
                        Literal["prepared", "pending", "accepted", "rejected"],
                        handoff.status,
                    )
                    if handoff is not None
                    else None
                ),
                sales_receiver_receipt_id=(
                    handoff.receiver_receipt_id if handoff is not None else None
                ),
                sales_handoff_rejection_code=(
                    handoff.rejection_code if handoff is not None else None
                ),
                sales_v2_version=(strategy_v2.version if strategy_v2 is not None else None),
                style_handoff_id=(style_handoff.id if style_handoff is not None else None),
                style_handoff_status=(
                    cast(
                        Literal["prepared", "pending", "accepted", "rejected"],
                        style_handoff.status,
                    )
                    if style_handoff is not None
                    else None
                ),
                style_decision_receipt_id=(
                    style_handoff.decision_receipt_id if style_handoff is not None else None
                ),
                style_rejection_code=(
                    style_handoff.rejection_code if style_handoff is not None else None
                ),
                style_eligibility_version=(
                    style_input.version if style_input is not None else None
                ),
                images=tuple(grouped[key]),
            )
            for key, (
                product,
                package,
                strategy,
                delivery,
                classification,
                strategy_v2,
                handoff,
                style_input,
                style_handoff,
            ) in products.items()
        ]

    async def image_source(
        self,
        tenant_id: UUID,
        product_code: str,
        package_version: int,
        asset_id: UUID,
    ) -> AlbumReviewImageSource | None:
        row = (
            await self.session.execute(
                select(SourceImage)
                .join(Product, Product.id == SourceImage.product_id)
                .join(
                    MaterialPackageAsset,
                    MaterialPackageAsset.source_image_id == SourceImage.id,
                )
                .join(MaterialPackage, MaterialPackage.id == MaterialPackageAsset.package_id)
                .where(
                    Product.tenant_id == tenant_id,
                    Product.product_code == product_code,
                    SourceImage.tenant_id == tenant_id,
                    SourceImage.upstream_asset_id == asset_id,
                    MaterialPackageAsset.tenant_id == tenant_id,
                    MaterialPackage.tenant_id == tenant_id,
                    MaterialPackage.version == package_version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return AlbumReviewImageSource(row.object_key, row.sha256, row.mime_type)


class AlbumReviewInbox:
    def __init__(self, store: AlbumReviewStore, objects: AlbumObjectReader) -> None:
        self.store = store
        self.objects = objects

    async def list_latest(self, tenant_id: UUID) -> Sequence[AlbumReviewProduct]:
        return await self.store.list_latest(tenant_id)

    async def read_image(
        self,
        tenant_id: UUID,
        product_code: str,
        package_version: int,
        asset_id: UUID,
    ) -> AlbumReviewImageContent:
        source = await self.store.image_source(
            tenant_id,
            product_code,
            package_version,
            asset_id,
        )
        if source is None:
            raise AlbumReviewInboxFailure("album_image_not_found")
        try:
            content = await self.objects.read(source.object_key)
        except ObjectValidationFailed:
            raise AlbumReviewInboxFailure("album_image_not_found") from None
        if hashlib.sha256(content).hexdigest() != source.sha256:
            raise AlbumReviewInboxFailure("album_image_digest_mismatch")
        return AlbumReviewImageContent(content, source.mime_type)
