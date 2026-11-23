from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from product_engine.contracts.sales_strategy import (
    SalesStrategyInputV1,
    canonical_sales_strategy_sha256,
)
from product_engine.models.products import Product, SalesStrategyInputRecord


@dataclass(frozen=True)
class AppendStrategyResult:
    value: SalesStrategyInputV1
    created: bool


def strategy_product_statement(
    tenant_id: UUID,
    product_code: str,
) -> Select[tuple[Product]]:
    return (
        select(Product)
        .where(
            Product.tenant_id == tenant_id,
            Product.product_code == product_code,
        )
        .with_for_update()
    )


def latest_strategy_statement(
    tenant_id: UUID,
    product_code: str,
) -> Select[tuple[SalesStrategyInputRecord]]:
    return (
        select(SalesStrategyInputRecord)
        .join(Product, Product.id == SalesStrategyInputRecord.product_id)
        .where(
            SalesStrategyInputRecord.tenant_id == tenant_id,
            Product.tenant_id == tenant_id,
            Product.product_code == product_code,
        )
        .order_by(SalesStrategyInputRecord.version.desc())
        .limit(1)
    )


def strategy_by_version_statement(
    tenant_id: UUID,
    product_code: str,
    version: int,
) -> Select[tuple[SalesStrategyInputRecord]]:
    return (
        select(SalesStrategyInputRecord)
        .join(Product, Product.id == SalesStrategyInputRecord.product_id)
        .where(
            SalesStrategyInputRecord.tenant_id == tenant_id,
            Product.tenant_id == tenant_id,
            Product.product_code == product_code,
            SalesStrategyInputRecord.version == version,
        )
    )


def strategy_by_content_statement(
    tenant_id: UUID,
    product_id: UUID,
    content_sha256: str,
) -> Select[tuple[SalesStrategyInputRecord]]:
    return select(SalesStrategyInputRecord).where(
        SalesStrategyInputRecord.tenant_id == tenant_id,
        SalesStrategyInputRecord.product_id == product_id,
        SalesStrategyInputRecord.content_sha256 == content_sha256,
    )


def latest_strategy_version_statement(
    tenant_id: UUID,
    product_id: UUID,
) -> Select[tuple[int]]:
    return select(func.max(SalesStrategyInputRecord.version)).where(
        SalesStrategyInputRecord.tenant_id == tenant_id,
        SalesStrategyInputRecord.product_id == product_id,
    )


class SalesStrategyInputRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append_preview(
        self,
        candidate: SalesStrategyInputV1,
    ) -> AppendStrategyResult:
        product = (
            await self.session.execute(
                strategy_product_statement(candidate.tenant_id, candidate.product_code)
            )
        ).scalar_one_or_none()
        if product is None:
            raise ValueError("strategy_product_not_found")

        digest = canonical_sales_strategy_sha256(candidate)
        existing = (
            await self.session.execute(
                strategy_by_content_statement(candidate.tenant_id, product.id, digest)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return AppendStrategyResult(
                value=SalesStrategyInputV1.model_validate(existing.payload),
                created=False,
            )

        latest_version = (
            await self.session.execute(
                latest_strategy_version_statement(candidate.tenant_id, product.id)
            )
        ).scalar_one_or_none()
        value = candidate.model_copy(
            update={
                "strategy_input_version": (latest_version or 0) + 1,
                "created_at": datetime.now(UTC),
                "content_sha256": digest,
            }
        )
        self.session.add(
            SalesStrategyInputRecord(
                id=uuid4(),
                tenant_id=value.tenant_id,
                product_id=product.id,
                version=value.strategy_input_version,
                product_data_version=value.product_data_version,
                source_album_event_id=value.source_album_event_id,
                source_material_package_version=value.source_material_package_version,
                source_image_set_sha256=value.source_image_set_sha256,
                content_sha256=value.content_sha256,
                status=value.status,
                payload=value.model_dump(mode="json"),
            )
        )
        await self.session.flush()
        return AppendStrategyResult(value=value, created=True)

    async def latest(
        self,
        tenant_id: UUID,
        product_code: str,
    ) -> SalesStrategyInputV1 | None:
        record = (
            await self.session.execute(latest_strategy_statement(tenant_id, product_code))
        ).scalar_one_or_none()
        return SalesStrategyInputV1.model_validate(record.payload) if record is not None else None

    async def by_version(
        self,
        tenant_id: UUID,
        product_code: str,
        version: int,
    ) -> SalesStrategyInputV1 | None:
        record = (
            await self.session.execute(
                strategy_by_version_statement(tenant_id, product_code, version)
            )
        ).scalar_one_or_none()
        return SalesStrategyInputV1.model_validate(record.payload) if record is not None else None
