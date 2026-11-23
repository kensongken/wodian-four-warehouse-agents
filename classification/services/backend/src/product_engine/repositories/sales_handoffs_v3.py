from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.contracts.sales_strategy_v3 import (
    SalesStrategyInputV3,
    SalesWarehouseDeliveryV2,
    SalesWarehouseReceiptV2,
    canonical_sales_strategy_v3_sha256,
)
from product_engine.models.classifications import (
    ProductClassificationAsset,
    ProductClassificationVersion,
)
from product_engine.models.material_evidence import (
    MaterialEvidenceRecord,
    MaterialEvidenceRevocationRecord,
)
from product_engine.models.products import Product, SourceImage
from product_engine.models.sales_handoffs_v3 import (
    SalesStrategyInputV3Record,
    SalesWarehouseReceiptV3Record,
    WarehouseHandoffV3Record,
)


@dataclass(frozen=True)
class SalesV3Asset:
    asset_id: UUID
    source_image_id: UUID
    position: int
    role: Literal["main_candidate", "gallery", "source_original"]
    sha256: str
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]


@dataclass(frozen=True)
class SalesV3Source:
    classification_id: UUID
    tenant_id: UUID
    product_id: UUID
    product_code: str
    version: int
    is_latest: bool
    base_material_package_version: int
    material_label: str
    claim_basis: str
    evidence_active: bool
    product_form: str
    inventory_status: Literal["in_stock", "reserved", "out_of_stock"] | None
    visual_observations: tuple[str, ...]
    selling_point_candidates: tuple[str, ...]
    price_status: Literal["pending", "human_confirmed"]
    price_candidate: dict[str, Any] | None
    service_rule_summaries: tuple[str, ...]
    content_sha256: str
    source_image_set_sha256: str
    assets: tuple[SalesV3Asset, ...]
    created_at: datetime


@dataclass(frozen=True)
class WarehouseHandoffV3View:
    handoff_id: UUID
    product_code: str
    target_warehouse: Literal["sales"]
    payload_version: int
    payload_sha256: str
    idempotency_key: str
    status: Literal["prepared", "pending", "accepted", "rejected"]
    receiver_receipt_id: UUID | None
    rejection_code: str | None
    prepared_at: datetime
    last_attempted_at: datetime | None
    settled_at: datetime | None


@dataclass(frozen=True)
class PreparedSalesPreviewV3:
    payload: SalesStrategyInputV3
    handoff: WarehouseHandoffV3View
    created: bool


@dataclass(frozen=True)
class StoredSalesHandoffV3:
    payload: SalesStrategyInputV3
    handoff: WarehouseHandoffV3View


class SalesHandoffRepositoryV3:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load_latest_source(
        self, tenant_id: UUID, classification_id: UUID
    ) -> SalesV3Source | None:
        row = (
            await self.session.execute(
                select(ProductClassificationVersion, Product)
                .join(Product, Product.id == ProductClassificationVersion.product_id)
                .where(
                    ProductClassificationVersion.tenant_id == tenant_id,
                    ProductClassificationVersion.id == classification_id,
                    Product.tenant_id == tenant_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        classification, product = row
        latest_version = await self.session.scalar(
            select(func.max(ProductClassificationVersion.version)).where(
                ProductClassificationVersion.tenant_id == tenant_id,
                ProductClassificationVersion.product_id == product.id,
            )
        )
        asset_rows = (
            await self.session.execute(
                select(ProductClassificationAsset, SourceImage)
                .join(SourceImage, SourceImage.id == ProductClassificationAsset.source_image_id)
                .where(
                    ProductClassificationAsset.tenant_id == tenant_id,
                    ProductClassificationAsset.classification_version_id == classification.id,
                    SourceImage.tenant_id == tenant_id,
                )
                .order_by(ProductClassificationAsset.position)
            )
        ).all()
        evidence_active = classification.material_evidence_id is not None
        if classification.material_evidence_id is not None:
            evidence = await self.session.scalar(
                select(MaterialEvidenceRecord.id).where(
                    MaterialEvidenceRecord.tenant_id == tenant_id,
                    MaterialEvidenceRecord.id == classification.material_evidence_id,
                )
            )
            revoked = await self.session.scalar(
                select(MaterialEvidenceRevocationRecord.id).where(
                    MaterialEvidenceRevocationRecord.tenant_id == tenant_id,
                    MaterialEvidenceRevocationRecord.evidence_id
                    == classification.material_evidence_id,
                )
            )
            evidence_active = evidence is not None and revoked is None
        claim_basis = classification.material_claim_basis
        if claim_basis is None:
            claim_basis = (
                "information_insufficient"
                if classification.material_evidence == "information_insufficient"
                else "legacy_unscoped_confirmation"
            )
        return SalesV3Source(
            classification_id=classification.id,
            tenant_id=tenant_id,
            product_id=product.id,
            product_code=product.product_code,
            version=classification.version,
            is_latest=classification.version == latest_version,
            base_material_package_version=classification.base_material_package_version,
            material_label=classification.material_label,
            claim_basis=claim_basis,
            evidence_active=evidence_active,
            product_form=classification.product_form,
            inventory_status=cast(Any, classification.inventory_status),
            visual_observations=tuple(classification.visual_observations),
            selling_point_candidates=tuple(classification.selling_point_candidates),
            price_status=cast(Any, classification.price_status),
            price_candidate=classification.price_candidate,
            service_rule_summaries=tuple(classification.service_rule_summaries),
            content_sha256=classification.content_sha256,
            source_image_set_sha256=classification.source_image_set_sha256,
            assets=tuple(
                SalesV3Asset(
                    asset_id=image.upstream_asset_id,
                    source_image_id=image.id,
                    position=asset.position,
                    role=cast(Any, asset.role),
                    sha256=asset.sha256,
                    mime_type=cast(Any, image.mime_type),
                )
                for asset, image in asset_rows
            ),
            created_at=classification.created_at,
        )

    async def append_preview(self, candidate: SalesStrategyInputV3) -> PreparedSalesPreviewV3:
        existing = await self._by_classification(
            candidate.tenant_id, candidate.source_classification_id
        )
        if existing is not None:
            return await self._prepared(existing, False)
        product = await self.session.scalar(
            select(Product)
            .where(
                Product.tenant_id == candidate.tenant_id,
                Product.product_code == candidate.product_code,
            )
            .with_for_update()
        )
        if product is None:
            raise ValueError("classification_not_found")
        existing = await self._by_classification(
            candidate.tenant_id, candidate.source_classification_id
        )
        if existing is not None:
            return await self._prepared(existing, False)
        version = (
            await self.session.scalar(
                select(func.max(SalesStrategyInputV3Record.version)).where(
                    SalesStrategyInputV3Record.tenant_id == candidate.tenant_id,
                    SalesStrategyInputV3Record.product_id == product.id,
                )
            )
            or 0
        ) + 1
        value = candidate.model_copy(
            update={"strategy_input_version": version, "created_at": datetime.now(UTC)}
        )
        value = value.model_copy(
            update={"content_sha256": canonical_sales_strategy_v3_sha256(value)}
        )
        strategy = SalesStrategyInputV3Record(
            id=uuid4(),
            tenant_id=value.tenant_id,
            product_id=product.id,
            version=version,
            source_material_package_version=value.source_material_package_version,
            source_classification_id=value.source_classification_id,
            source_classification_version=value.source_classification_version,
            source_classification_sha256=value.source_classification_sha256,
            source_image_set_sha256=value.source_image_set_sha256,
            content_sha256=value.content_sha256,
            status=value.status,
            payload=value.model_dump(mode="json"),
            created_at=value.created_at,
        )
        handoff = WarehouseHandoffV3Record(
            id=uuid4(),
            tenant_id=value.tenant_id,
            product_id=product.id,
            strategy_input_id=strategy.id,
            target_warehouse="sales",
            payload_version=version,
            payload_sha256=value.content_sha256,
            idempotency_key=f"sales-v3:{value.tenant_id}:{value.source_classification_id}",
            status="prepared",
            prepared_at=value.created_at,
        )
        try:
            async with self.session.begin_nested():
                self.session.add_all([strategy, handoff])
                await self.session.flush()
        except IntegrityError:
            recovered = await self._by_classification(
                candidate.tenant_id, candidate.source_classification_id
            )
            if recovered is None:
                raise
            return await self._prepared(recovered, False)
        return PreparedSalesPreviewV3(value, self._view(handoff, product.product_code), True)

    async def read_handoff(self, tenant_id: UUID, handoff_id: UUID) -> StoredSalesHandoffV3 | None:
        row = (
            await self.session.execute(
                select(WarehouseHandoffV3Record, SalesStrategyInputV3Record, Product)
                .join(
                    SalesStrategyInputV3Record,
                    SalesStrategyInputV3Record.id == WarehouseHandoffV3Record.strategy_input_id,
                )
                .join(Product, Product.id == WarehouseHandoffV3Record.product_id)
                .where(
                    WarehouseHandoffV3Record.tenant_id == tenant_id,
                    WarehouseHandoffV3Record.id == handoff_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        handoff, strategy, product = row
        return StoredSalesHandoffV3(
            SalesStrategyInputV3.model_validate(strategy.payload),
            self._view(handoff, product.product_code),
        )

    async def mark_pending_and_build_delivery(
        self, tenant_id: UUID, handoff_id: UUID
    ) -> SalesWarehouseDeliveryV2:
        stored = await self.read_handoff(tenant_id, handoff_id)
        if stored is None:
            raise ValueError("warehouse_handoff_not_found")
        handoff = await self.session.get(WarehouseHandoffV3Record, handoff_id, with_for_update=True)
        assert handoff is not None
        handoff.status = "pending"
        handoff.last_attempted_at = datetime.now(UTC)
        await self.session.flush()
        return SalesWarehouseDeliveryV2(
            handoff_id=handoff.id,
            idempotency_key=handoff.idempotency_key,
            payload_sha256=handoff.payload_sha256,
            payload=stored.payload,
        )

    async def accept_delivery(
        self, tenant_id: UUID, delivery: SalesWarehouseDeliveryV2
    ) -> SalesWarehouseReceiptV2:
        existing = await self.session.scalar(
            select(SalesWarehouseReceiptV3Record).where(
                SalesWarehouseReceiptV3Record.tenant_id == tenant_id,
                SalesWarehouseReceiptV3Record.idempotency_key == delivery.idempotency_key,
            )
        )
        if existing is not None:
            if existing.accepted_payload_sha256 != delivery.payload_sha256:
                raise ValueError("sales_handoff_conflict")
            return SalesWarehouseReceiptV2.model_validate(existing.payload)
        accepted_at = datetime.now(UTC)
        receipt = SalesWarehouseReceiptV2(
            receiver_receipt_id=uuid4(),
            handoff_id=delivery.handoff_id,
            accepted_payload_sha256=delivery.payload_sha256,
            accepted_at=accepted_at,
        )
        record = SalesWarehouseReceiptV3Record(
            id=receipt.receiver_receipt_id,
            tenant_id=tenant_id,
            handoff_id=delivery.handoff_id,
            idempotency_key=delivery.idempotency_key,
            accepted_payload_sha256=delivery.payload_sha256,
            payload=receipt.model_dump(mode="json"),
            accepted_at=accepted_at,
        )
        handoff = await self.session.get(
            WarehouseHandoffV3Record, delivery.handoff_id, with_for_update=True
        )
        if handoff is None or handoff.tenant_id != tenant_id:
            raise ValueError("warehouse_handoff_not_found")
        handoff.status = "accepted"
        handoff.receiver_receipt_id = receipt.receiver_receipt_id
        handoff.settled_at = accepted_at
        self.session.add(record)
        await self.session.flush()
        return receipt

    async def _by_classification(
        self, tenant_id: UUID, classification_id: UUID
    ) -> SalesStrategyInputV3Record | None:
        return (
            await self.session.execute(
                select(SalesStrategyInputV3Record).where(
                    SalesStrategyInputV3Record.tenant_id == tenant_id,
                    SalesStrategyInputV3Record.source_classification_id == classification_id,
                )
            )
        ).scalar_one_or_none()

    async def _prepared(
        self, record: SalesStrategyInputV3Record, created: bool
    ) -> PreparedSalesPreviewV3:
        row = (
            await self.session.execute(
                select(WarehouseHandoffV3Record, Product)
                .join(Product, Product.id == WarehouseHandoffV3Record.product_id)
                .where(WarehouseHandoffV3Record.strategy_input_id == record.id)
            )
        ).one()
        handoff, product = row
        return PreparedSalesPreviewV3(
            SalesStrategyInputV3.model_validate(record.payload),
            self._view(handoff, product.product_code),
            created,
        )

    @staticmethod
    def _view(record: WarehouseHandoffV3Record, product_code: str) -> WarehouseHandoffV3View:
        return WarehouseHandoffV3View(
            record.id,
            product_code,
            "sales",
            record.payload_version,
            record.payload_sha256,
            record.idempotency_key,
            cast(Any, record.status),
            record.receiver_receipt_id,
            record.rejection_code,
            record.prepared_at,
            record.last_attempted_at,
            record.settled_at,
        )
