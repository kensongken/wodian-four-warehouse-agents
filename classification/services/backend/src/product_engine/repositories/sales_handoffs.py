import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.contracts.sales_strategy_v2 import (
    SalesStrategyInputV2,
    SalesWarehouseDeliveryV1,
    SalesWarehouseReceiptV1,
    canonical_sales_strategy_v2_sha256,
)
from product_engine.models.classifications import (
    ProductClassificationAsset,
    ProductClassificationVersion,
)
from product_engine.models.products import Product, SourceImage
from product_engine.models.sales_handoffs import (
    SalesStrategyInputV2Record,
    SalesWarehouseReceiptRecord,
    WarehouseHandoffRecord,
)


class SalesHandoffRepositoryFailure(ValueError):
    pass


@dataclass(frozen=True)
class AuthoritativeClassificationAsset:
    asset_id: UUID
    source_image_id: UUID
    position: int
    role: Literal["main_candidate", "gallery", "source_original"]
    sha256: str
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]


@dataclass(frozen=True)
class AuthoritativeClassificationSource:
    classification_id: UUID
    tenant_id: UUID
    product_id: UUID
    product_code: str
    version: int
    is_latest: bool
    base_material_package_version: int
    material_label: str
    material_evidence: Literal["human_confirmed", "information_insufficient"]
    product_form: str
    main_source_image_id: UUID
    visual_observations: tuple[str, ...]
    selling_point_candidates: tuple[str, ...]
    inventory_status: Literal["in_stock", "reserved", "out_of_stock"] | None
    price_status: Literal["pending", "human_confirmed"]
    price_candidate: dict[str, Any] | None
    service_rule_summaries: tuple[str, ...]
    review_source: str
    request_id: UUID
    content_sha256: str
    source_image_set_sha256: str
    assets: tuple[AuthoritativeClassificationAsset, ...]


@dataclass(frozen=True)
class WarehouseHandoffView:
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
class PreparedSalesPreview:
    payload: SalesStrategyInputV2
    handoff: WarehouseHandoffView
    created: bool


@dataclass(frozen=True)
class StoredSalesHandoff:
    payload: SalesStrategyInputV2
    handoff: WarehouseHandoffView


def classification_image_set_sha256(
    assets: tuple[AuthoritativeClassificationAsset, ...],
) -> str:
    body = [
        {
            "asset_id": str(asset.asset_id),
            "position": asset.position,
            "role": asset.role,
            "sha256": asset.sha256,
        }
        for asset in assets
    ]
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class SalesHandoffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load_latest_source(
        self, tenant_id: UUID, classification_id: UUID
    ) -> AuthoritativeClassificationSource | None:
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
                .join(
                    SourceImage,
                    SourceImage.id == ProductClassificationAsset.source_image_id,
                )
                .where(
                    ProductClassificationAsset.tenant_id == tenant_id,
                    ProductClassificationAsset.classification_version_id == classification.id,
                    SourceImage.tenant_id == tenant_id,
                )
                .order_by(ProductClassificationAsset.position)
            )
        ).all()
        try:
            assets = tuple(self._classification_asset(asset, image) for asset, image in asset_rows)
        except ValueError as error:
            raise SalesHandoffRepositoryFailure("classification_ledger_conflict") from error
        return AuthoritativeClassificationSource(
            classification_id=classification.id,
            tenant_id=tenant_id,
            product_id=product.id,
            product_code=product.product_code,
            version=classification.version,
            is_latest=classification.version == latest_version,
            base_material_package_version=classification.base_material_package_version,
            material_label=classification.material_label,
            material_evidence=classification.material_evidence,
            product_form=classification.product_form,
            main_source_image_id=classification.main_asset_id,
            visual_observations=tuple(classification.visual_observations),
            selling_point_candidates=tuple(classification.selling_point_candidates),
            inventory_status=classification.inventory_status,
            price_status=classification.price_status,
            price_candidate=classification.price_candidate,
            service_rule_summaries=tuple(classification.service_rule_summaries),
            review_source=classification.review_source,
            request_id=classification.request_id,
            content_sha256=classification.content_sha256,
            source_image_set_sha256=classification.source_image_set_sha256,
            assets=assets,
        )

    async def append_preview(self, candidate: SalesStrategyInputV2) -> PreparedSalesPreview:
        existing = await self._by_classification(
            candidate.tenant_id, candidate.source_classification_id
        )
        if existing is not None:
            return await self._prepared(existing, created=False)

        await self.session.execute(
            select(Product.id)
            .where(
                Product.tenant_id == candidate.tenant_id,
                Product.product_code == candidate.product_code,
            )
            .with_for_update()
        )
        existing = await self._by_classification(
            candidate.tenant_id, candidate.source_classification_id
        )
        if existing is not None:
            return await self._prepared(existing, created=False)

        product_id = await self.session.scalar(
            select(Product.id).where(
                Product.tenant_id == candidate.tenant_id,
                Product.product_code == candidate.product_code,
            )
        )
        if product_id is None:
            raise SalesHandoffRepositoryFailure("classification_not_found")
        latest_version = await self.session.scalar(
            select(func.max(SalesStrategyInputV2Record.version)).where(
                SalesStrategyInputV2Record.tenant_id == candidate.tenant_id,
                SalesStrategyInputV2Record.product_id == product_id,
            )
        )
        value = candidate.model_copy(
            update={
                "strategy_input_version": (latest_version or 0) + 1,
                "created_at": datetime.now(UTC),
            }
        )
        value = value.model_copy(
            update={"content_sha256": canonical_sales_strategy_v2_sha256(value)}
        )
        strategy_id = uuid4()
        handoff_id = uuid4()
        record = SalesStrategyInputV2Record(
            id=strategy_id,
            tenant_id=value.tenant_id,
            product_id=product_id,
            version=value.strategy_input_version,
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
        handoff = WarehouseHandoffRecord(
            id=handoff_id,
            tenant_id=value.tenant_id,
            product_id=product_id,
            strategy_input_id=strategy_id,
            target_warehouse="sales",
            payload_version=value.strategy_input_version,
            payload_sha256=value.content_sha256,
            idempotency_key=(f"sales:{value.tenant_id}:{value.source_classification_id}"),
            status="prepared",
            prepared_at=value.created_at,
        )
        try:
            async with self.session.begin_nested():
                self.session.add_all([record, handoff])
                await self.session.flush()
        except IntegrityError as error:
            recovered = await self._by_classification(
                candidate.tenant_id, candidate.source_classification_id
            )
            if recovered is None:
                raise SalesHandoffRepositoryFailure("sales_strategy_v2_conflict") from error
            return await self._prepared(recovered, created=False)
        return PreparedSalesPreview(
            payload=value,
            handoff=self._handoff_view(handoff, value.product_code),
            created=True,
        )

    async def read_handoff(self, tenant_id: UUID, handoff_id: UUID) -> StoredSalesHandoff | None:
        row = (
            await self.session.execute(
                select(WarehouseHandoffRecord, SalesStrategyInputV2Record, Product)
                .join(
                    SalesStrategyInputV2Record,
                    SalesStrategyInputV2Record.id == WarehouseHandoffRecord.strategy_input_id,
                )
                .join(Product, Product.id == WarehouseHandoffRecord.product_id)
                .where(
                    WarehouseHandoffRecord.tenant_id == tenant_id,
                    WarehouseHandoffRecord.id == handoff_id,
                    SalesStrategyInputV2Record.tenant_id == tenant_id,
                    Product.tenant_id == tenant_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        handoff, strategy, product = row
        return StoredSalesHandoff(
            payload=SalesStrategyInputV2.model_validate(strategy.payload),
            handoff=self._handoff_view(handoff, product.product_code),
        )

    async def mark_pending_and_build_delivery(
        self, tenant_id: UUID, handoff_id: UUID
    ) -> SalesWarehouseDeliveryV1:
        row = (
            await self.session.execute(
                select(WarehouseHandoffRecord, SalesStrategyInputV2Record)
                .join(
                    SalesStrategyInputV2Record,
                    SalesStrategyInputV2Record.id == WarehouseHandoffRecord.strategy_input_id,
                )
                .where(
                    WarehouseHandoffRecord.tenant_id == tenant_id,
                    WarehouseHandoffRecord.id == handoff_id,
                    SalesStrategyInputV2Record.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise SalesHandoffRepositoryFailure("warehouse_handoff_not_found")
        handoff, strategy = row
        if handoff.status == "rejected":
            raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict")
        payload = SalesStrategyInputV2.model_validate(strategy.payload)
        if (
            payload.content_sha256 != handoff.payload_sha256
            or canonical_sales_strategy_v2_sha256(payload) != handoff.payload_sha256
        ):
            raise SalesHandoffRepositoryFailure("sales_payload_hash_mismatch")
        if handoff.status != "accepted":
            handoff.status = "pending"
            handoff.last_attempted_at = datetime.now(UTC)
            await self.session.flush()
        return SalesWarehouseDeliveryV1(
            handoff_id=handoff.id,
            idempotency_key=handoff.idempotency_key,
            payload_sha256=handoff.payload_sha256,
            payload=payload,
        )

    async def accept_handoff(
        self,
        tenant_id: UUID,
        handoff_id: UUID,
        receipt: SalesWarehouseReceiptV1,
    ) -> WarehouseHandoffView:
        row = (
            await self.session.execute(
                select(WarehouseHandoffRecord, Product)
                .join(Product, Product.id == WarehouseHandoffRecord.product_id)
                .where(
                    WarehouseHandoffRecord.tenant_id == tenant_id,
                    WarehouseHandoffRecord.id == handoff_id,
                    Product.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise SalesHandoffRepositoryFailure("warehouse_handoff_not_found")
        handoff, product = row
        if (
            receipt.handoff_id != handoff.id
            or receipt.accepted_payload_sha256 != handoff.payload_sha256
        ):
            raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict")
        if handoff.status == "accepted":
            if handoff.receiver_receipt_id != receipt.receiver_receipt_id:
                raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict")
            return self._handoff_view(handoff, product.product_code)
        if handoff.status != "pending":
            raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict")
        handoff.status = "accepted"
        handoff.receiver_receipt_id = receipt.receiver_receipt_id
        handoff.rejection_code = None
        handoff.settled_at = receipt.accepted_at
        await self.session.flush()
        return self._handoff_view(handoff, product.product_code)

    async def reject_handoff(
        self, tenant_id: UUID, handoff_id: UUID, rejection_code: str
    ) -> WarehouseHandoffView:
        row = (
            await self.session.execute(
                select(WarehouseHandoffRecord, Product)
                .join(Product, Product.id == WarehouseHandoffRecord.product_id)
                .where(
                    WarehouseHandoffRecord.tenant_id == tenant_id,
                    WarehouseHandoffRecord.id == handoff_id,
                    Product.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise SalesHandoffRepositoryFailure("warehouse_handoff_not_found")
        handoff, product = row
        if handoff.status == "accepted":
            raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict")
        handoff.status = "rejected"
        handoff.rejection_code = rejection_code
        handoff.settled_at = datetime.now(UTC)
        await self.session.flush()
        return self._handoff_view(handoff, product.product_code)

    async def _by_classification(
        self, tenant_id: UUID, classification_id: UUID
    ) -> SalesStrategyInputV2Record | None:
        return (
            await self.session.execute(
                select(SalesStrategyInputV2Record).where(
                    SalesStrategyInputV2Record.tenant_id == tenant_id,
                    SalesStrategyInputV2Record.source_classification_id == classification_id,
                )
            )
        ).scalar_one_or_none()

    async def _prepared(
        self, record: SalesStrategyInputV2Record, *, created: bool
    ) -> PreparedSalesPreview:
        handoff = (
            await self.session.execute(
                select(WarehouseHandoffRecord).where(
                    WarehouseHandoffRecord.tenant_id == record.tenant_id,
                    WarehouseHandoffRecord.strategy_input_id == record.id,
                    WarehouseHandoffRecord.target_warehouse == "sales",
                )
            )
        ).scalar_one_or_none()
        if handoff is None:
            raise SalesHandoffRepositoryFailure("sales_strategy_v2_conflict")
        product_code = await self.session.scalar(
            select(Product.product_code).where(
                Product.tenant_id == record.tenant_id,
                Product.id == record.product_id,
            )
        )
        if product_code is None:
            raise SalesHandoffRepositoryFailure("sales_strategy_v2_conflict")
        return PreparedSalesPreview(
            payload=SalesStrategyInputV2.model_validate(record.payload),
            handoff=self._handoff_view(handoff, product_code),
            created=created,
        )

    @staticmethod
    def _handoff_view(record: WarehouseHandoffRecord, product_code: str) -> WarehouseHandoffView:
        if record.status not in {"prepared", "pending", "accepted", "rejected"}:
            raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict")
        return WarehouseHandoffView(
            handoff_id=record.id,
            product_code=product_code,
            target_warehouse="sales",
            payload_version=record.payload_version,
            payload_sha256=record.payload_sha256,
            idempotency_key=record.idempotency_key,
            status=cast(
                Literal["prepared", "pending", "accepted", "rejected"],
                record.status,
            ),
            receiver_receipt_id=record.receiver_receipt_id,
            rejection_code=record.rejection_code,
            prepared_at=record.prepared_at,
            last_attempted_at=record.last_attempted_at,
            settled_at=record.settled_at,
        )

    @staticmethod
    def _classification_asset(
        asset: ProductClassificationAsset,
        image: SourceImage,
    ) -> AuthoritativeClassificationAsset:
        if asset.role not in {"main_candidate", "gallery", "source_original"}:
            raise SalesHandoffRepositoryFailure("classification_ledger_conflict")
        if image.mime_type not in {"image/heic", "image/jpeg", "image/png"}:
            raise SalesHandoffRepositoryFailure("classification_ledger_conflict")
        return AuthoritativeClassificationAsset(
            asset_id=image.upstream_asset_id,
            source_image_id=image.id,
            position=asset.position,
            role=cast(
                Literal["main_candidate", "gallery", "source_original"],
                asset.role,
            ),
            sha256=asset.sha256,
            mime_type=cast(Literal["image/heic", "image/jpeg", "image/png"], image.mime_type),
        )


class SalesWarehouseReceiptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def accept(
        self, tenant_id: UUID, delivery: SalesWarehouseDeliveryV1
    ) -> SalesWarehouseReceiptV1:
        existing = await self._by_key(tenant_id, delivery.idempotency_key)
        if existing is not None:
            return self._replay(existing, delivery)
        handoff = (
            await self.session.execute(
                select(WarehouseHandoffRecord).where(
                    WarehouseHandoffRecord.tenant_id == tenant_id,
                    WarehouseHandoffRecord.id == delivery.handoff_id,
                )
            )
        ).scalar_one_or_none()
        if (
            handoff is None
            or handoff.status not in {"pending", "accepted"}
            or handoff.idempotency_key != delivery.idempotency_key
            or handoff.payload_sha256 != delivery.payload_sha256
        ):
            raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict")
        record = SalesWarehouseReceiptRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            handoff_id=delivery.handoff_id,
            idempotency_key=delivery.idempotency_key,
            accepted_payload_sha256=delivery.payload_sha256,
            payload=delivery.payload.model_dump(mode="json"),
            accepted_at=datetime.now(UTC),
        )
        try:
            async with self.session.begin_nested():
                self.session.add(record)
                await self.session.flush()
        except IntegrityError as error:
            recovered = await self._by_key(tenant_id, delivery.idempotency_key)
            if recovered is None:
                raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict") from error
            return self._replay(recovered, delivery)
        return self._receipt(record)

    async def _by_key(
        self, tenant_id: UUID, idempotency_key: str
    ) -> SalesWarehouseReceiptRecord | None:
        return (
            await self.session.execute(
                select(SalesWarehouseReceiptRecord).where(
                    SalesWarehouseReceiptRecord.tenant_id == tenant_id,
                    SalesWarehouseReceiptRecord.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()

    def _replay(
        self,
        record: SalesWarehouseReceiptRecord,
        delivery: SalesWarehouseDeliveryV1,
    ) -> SalesWarehouseReceiptV1:
        if (
            record.handoff_id != delivery.handoff_id
            or record.accepted_payload_sha256 != delivery.payload_sha256
            or record.payload != delivery.payload.model_dump(mode="json")
        ):
            raise SalesHandoffRepositoryFailure("warehouse_handoff_conflict")
        return self._receipt(record)

    @staticmethod
    def _receipt(record: SalesWarehouseReceiptRecord) -> SalesWarehouseReceiptV1:
        return SalesWarehouseReceiptV1(
            receiver_receipt_id=record.id,
            handoff_id=record.handoff_id,
            accepted_payload_sha256=record.accepted_payload_sha256,
            accepted_at=record.accepted_at,
        )
