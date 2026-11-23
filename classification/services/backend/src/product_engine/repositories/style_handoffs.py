import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityInputV2,
    StyleWarehouseDecisionReceiptV1,
    StyleWarehouseDeliveryV1,
    canonical_style_eligibility_v2_sha256,
)
from product_engine.models.classifications import (
    ProductClassificationAsset,
    ProductClassificationVersion,
)
from product_engine.models.products import Product, SourceImage
from product_engine.models.style_handoffs import (
    StyleEligibilityInputV2Record,
    StyleTransformAuthorizationRecord,
    StyleWarehouseDecisionReceiptRecord,
    StyleWarehouseHandoffRecord,
)

Now = Callable[[], datetime]


class StyleHandoffRepositoryFailure(ValueError):
    pass


@dataclass(frozen=True)
class StyleClassificationAsset:
    asset_id: UUID
    source_image_id: UUID
    position: int
    role: Literal["main_candidate", "gallery", "source_original"]
    sha256: str
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]


@dataclass(frozen=True)
class StyleClassificationSource:
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
    assets: tuple[StyleClassificationAsset, ...]


@dataclass(frozen=True)
class StyleTransformAuthorizationView:
    authorization_id: UUID
    authorization_request_id: UUID
    tenant_id: UUID
    classification_id: UUID
    source_image_set_sha256: str
    scope: Literal["local_style_acceptance_only"]
    expires_at: datetime
    authorized_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class StyleHandoffView:
    handoff_id: UUID
    product_code: str
    payload_version: int
    payload_sha256: str
    idempotency_key: str
    status: Literal["prepared", "pending", "accepted", "rejected"]
    decision_receipt_id: UUID | None
    rejection_code: str | None
    prepared_at: datetime
    last_attempted_at: datetime | None
    settled_at: datetime | None


@dataclass(frozen=True)
class PreparedStyleEligibility:
    eligibility_input_id: UUID
    payload: StyleEligibilityInputV2
    handoff: StyleHandoffView
    created: bool


@dataclass(frozen=True)
class StoredStyleHandoff:
    eligibility_input_id: UUID
    payload: StyleEligibilityInputV2
    handoff: StyleHandoffView


@dataclass(frozen=True)
class StyleAuthoritativeGateState:
    classification_id: UUID
    classification_version: int
    classification_sha256: str
    source_image_set_sha256: str
    material_evidence: Literal["human_confirmed", "information_insufficient"]
    authorization: StyleTransformAuthorizationView | None


def classification_image_set_sha256(
    assets: tuple[StyleClassificationAsset, ...],
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


def authorization_request_sha256(
    *,
    classification_id: UUID,
    source_image_set_sha256: str,
    expires_at: datetime,
) -> str:
    body = {
        "classification_id": str(classification_id),
        "expires_at": expires_at.isoformat(),
        "scope": "local_style_acceptance_only",
        "source_image_set_sha256": source_image_set_sha256,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class StyleHandoffRepository:
    def __init__(self, session: AsyncSession, *, now: Now | None = None) -> None:
        self.session = session
        self.now = now or (lambda: datetime.now(UTC))

    async def load_latest_source(
        self, tenant_id: UUID, classification_id: UUID
    ) -> StyleClassificationSource | None:
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
            raise StyleHandoffRepositoryFailure("classification_ledger_conflict") from error
        return StyleClassificationSource(
            classification_id=classification.id,
            tenant_id=tenant_id,
            product_id=product.id,
            product_code=product.product_code,
            version=classification.version,
            is_latest=classification.version == latest_version,
            base_material_package_version=classification.base_material_package_version,
            material_label=classification.material_label,
            material_evidence=cast(
                Literal["human_confirmed", "information_insufficient"],
                classification.material_evidence,
            ),
            product_form=classification.product_form,
            main_source_image_id=classification.main_asset_id,
            visual_observations=tuple(classification.visual_observations),
            selling_point_candidates=tuple(classification.selling_point_candidates),
            inventory_status=cast(
                Literal["in_stock", "reserved", "out_of_stock"] | None,
                classification.inventory_status,
            ),
            price_status=cast(Literal["pending", "human_confirmed"], classification.price_status),
            price_candidate=classification.price_candidate,
            service_rule_summaries=tuple(classification.service_rule_summaries),
            review_source=classification.review_source,
            request_id=classification.request_id,
            content_sha256=classification.content_sha256,
            source_image_set_sha256=classification.source_image_set_sha256,
            assets=assets,
        )

    async def load_latest_authorization(
        self,
        tenant_id: UUID,
        classification_id: UUID,
        source_image_set_sha256: str,
    ) -> StyleTransformAuthorizationView | None:
        record = (
            await self.session.execute(
                select(StyleTransformAuthorizationRecord)
                .where(
                    StyleTransformAuthorizationRecord.tenant_id == tenant_id,
                    StyleTransformAuthorizationRecord.classification_id == classification_id,
                    StyleTransformAuthorizationRecord.source_image_set_sha256
                    == source_image_set_sha256,
                    StyleTransformAuthorizationRecord.scope == "local_style_acceptance_only",
                )
                .order_by(
                    StyleTransformAuthorizationRecord.revoked_at.asc().nulls_first(),
                    StyleTransformAuthorizationRecord.authorized_at.desc(),
                    StyleTransformAuthorizationRecord.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return self._authorization_view(record) if record is not None else None

    async def append_authorization(
        self,
        tenant_id: UUID,
        classification_id: UUID,
        *,
        authorization_request_id: UUID,
        expires_at: datetime,
    ) -> StyleTransformAuthorizationView:
        source = await self.load_latest_source(tenant_id, classification_id)
        if source is None:
            raise StyleHandoffRepositoryFailure("classification_not_found")
        if not source.is_latest:
            raise StyleHandoffRepositoryFailure("classification_not_latest")
        request_sha256 = authorization_request_sha256(
            classification_id=classification_id,
            source_image_set_sha256=source.source_image_set_sha256,
            expires_at=expires_at,
        )
        existing = await self._authorization_by_request(tenant_id, authorization_request_id)
        if existing is not None:
            return self._replay_authorization(existing, request_sha256)
        await self.session.execute(
            select(ProductClassificationVersion.id)
            .where(
                ProductClassificationVersion.tenant_id == tenant_id,
                ProductClassificationVersion.id == classification_id,
            )
            .with_for_update()
        )
        existing = await self._authorization_by_request(tenant_id, authorization_request_id)
        if existing is not None:
            return self._replay_authorization(existing, request_sha256)
        record = StyleTransformAuthorizationRecord(
            id=uuid4(),
            authorization_request_id=authorization_request_id,
            tenant_id=tenant_id,
            classification_id=classification_id,
            source_image_set_sha256=source.source_image_set_sha256,
            scope="local_style_acceptance_only",
            request_sha256=request_sha256,
            expires_at=expires_at,
            authorized_at=self.now(),
        )
        try:
            async with self.session.begin_nested():
                self.session.add(record)
                await self.session.flush()
        except IntegrityError as error:
            recovered = await self._authorization_by_request(tenant_id, authorization_request_id)
            if recovered is None:
                raise StyleHandoffRepositoryFailure("style_authorization_conflict") from error
            return self._replay_authorization(recovered, request_sha256)
        return self._authorization_view(record)

    async def revoke_authorization(
        self, tenant_id: UUID, authorization_id: UUID
    ) -> StyleTransformAuthorizationView:
        record = (
            await self.session.execute(
                select(StyleTransformAuthorizationRecord)
                .where(
                    StyleTransformAuthorizationRecord.tenant_id == tenant_id,
                    StyleTransformAuthorizationRecord.id == authorization_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            raise StyleHandoffRepositoryFailure("style_authorization_not_found")
        if record.revoked_at is None:
            record.revoked_at = self.now()
            await self.session.flush()
        return self._authorization_view(record)

    async def append_eligibility(
        self, candidate: StyleEligibilityInputV2
    ) -> PreparedStyleEligibility:
        product_id = await self.session.scalar(
            select(Product.id).where(
                Product.tenant_id == candidate.tenant_id,
                Product.product_code == candidate.product_code,
            )
        )
        if product_id is None:
            raise StyleHandoffRepositoryFailure("classification_not_found")
        existing = await self._input_by_content(
            candidate.tenant_id, product_id, candidate.content_sha256
        )
        if existing is not None:
            return await self._prepared(existing, created=False)
        await self.session.execute(
            select(Product.id)
            .where(
                Product.tenant_id == candidate.tenant_id,
                Product.id == product_id,
            )
            .with_for_update()
        )
        existing = await self._input_by_content(
            candidate.tenant_id, product_id, candidate.content_sha256
        )
        if existing is not None:
            return await self._prepared(existing, created=False)
        latest_version = await self.session.scalar(
            select(func.max(StyleEligibilityInputV2Record.version)).where(
                StyleEligibilityInputV2Record.tenant_id == candidate.tenant_id,
                StyleEligibilityInputV2Record.product_id == product_id,
            )
        )
        value = candidate.model_copy(
            update={
                "eligibility_input_version": (latest_version or 0) + 1,
                "created_at": self.now(),
            }
        )
        value = value.model_copy(
            update={"content_sha256": canonical_style_eligibility_v2_sha256(value)}
        )
        eligibility_input_id = uuid4()
        handoff_id = uuid4()
        record = StyleEligibilityInputV2Record(
            id=eligibility_input_id,
            tenant_id=value.tenant_id,
            product_id=product_id,
            version=value.eligibility_input_version,
            source_material_package_version=value.source_material_package_version,
            source_classification_id=value.source_classification_id,
            source_classification_version=value.source_classification_version,
            source_classification_sha256=value.source_classification_sha256,
            source_image_set_sha256=value.source_image_set_sha256,
            transform_authorization_id=value.transform_authorization_id,
            transform_authorization_status=value.transform_authorization_status,
            content_sha256=value.content_sha256,
            status=value.status,
            payload=value.model_dump(mode="json"),
            created_at=value.created_at,
        )
        handoff = StyleWarehouseHandoffRecord(
            id=handoff_id,
            tenant_id=value.tenant_id,
            product_id=product_id,
            eligibility_input_id=eligibility_input_id,
            payload_version=value.eligibility_input_version,
            payload_sha256=value.content_sha256,
            idempotency_key=(f"style:{value.tenant_id}:{eligibility_input_id}"),
            status="prepared",
            prepared_at=value.created_at,
        )
        try:
            async with self.session.begin_nested():
                self.session.add_all([record, handoff])
                await self.session.flush()
        except IntegrityError as error:
            recovered = await self._input_by_content(
                candidate.tenant_id, product_id, candidate.content_sha256
            )
            if recovered is None:
                raise StyleHandoffRepositoryFailure("style_eligibility_conflict") from error
            return await self._prepared(recovered, created=False)
        return PreparedStyleEligibility(
            eligibility_input_id=eligibility_input_id,
            payload=value,
            handoff=self._handoff_view(handoff, value.product_code),
            created=True,
        )

    async def read_handoff(self, tenant_id: UUID, handoff_id: UUID) -> StoredStyleHandoff | None:
        row = (
            await self.session.execute(
                select(StyleWarehouseHandoffRecord, StyleEligibilityInputV2Record, Product)
                .join(
                    StyleEligibilityInputV2Record,
                    StyleEligibilityInputV2Record.id
                    == StyleWarehouseHandoffRecord.eligibility_input_id,
                )
                .join(Product, Product.id == StyleWarehouseHandoffRecord.product_id)
                .where(
                    StyleWarehouseHandoffRecord.tenant_id == tenant_id,
                    StyleWarehouseHandoffRecord.id == handoff_id,
                    StyleEligibilityInputV2Record.tenant_id == tenant_id,
                    Product.tenant_id == tenant_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        handoff, eligibility, product = row
        return StoredStyleHandoff(
            eligibility_input_id=eligibility.id,
            payload=StyleEligibilityInputV2.model_validate(eligibility.payload),
            handoff=self._handoff_view(handoff, product.product_code),
        )

    async def mark_pending_and_build_delivery(
        self, tenant_id: UUID, handoff_id: UUID
    ) -> StyleWarehouseDeliveryV1:
        row = (
            await self.session.execute(
                select(StyleWarehouseHandoffRecord, StyleEligibilityInputV2Record)
                .join(
                    StyleEligibilityInputV2Record,
                    StyleEligibilityInputV2Record.id
                    == StyleWarehouseHandoffRecord.eligibility_input_id,
                )
                .where(
                    StyleWarehouseHandoffRecord.tenant_id == tenant_id,
                    StyleWarehouseHandoffRecord.id == handoff_id,
                    StyleEligibilityInputV2Record.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise StyleHandoffRepositoryFailure("style_handoff_not_found")
        handoff, eligibility = row
        payload = StyleEligibilityInputV2.model_validate(eligibility.payload)
        if (
            payload.content_sha256 != handoff.payload_sha256
            or canonical_style_eligibility_v2_sha256(payload) != handoff.payload_sha256
        ):
            raise StyleHandoffRepositoryFailure("style_payload_hash_mismatch")
        if handoff.status not in {"accepted", "rejected"}:
            handoff.status = "pending"
            handoff.last_attempted_at = self.now()
            await self.session.flush()
        return StyleWarehouseDeliveryV1(
            handoff_id=handoff.id,
            eligibility_input_id=eligibility.id,
            idempotency_key=handoff.idempotency_key,
            payload_sha256=handoff.payload_sha256,
            payload=payload,
        )

    async def authoritative_gate_state(
        self, tenant_id: UUID, delivery: StyleWarehouseDeliveryV1
    ) -> StyleAuthoritativeGateState:
        handoff = (
            await self.session.execute(
                select(StyleWarehouseHandoffRecord).where(
                    StyleWarehouseHandoffRecord.tenant_id == tenant_id,
                    StyleWarehouseHandoffRecord.id == delivery.handoff_id,
                )
            )
        ).scalar_one_or_none()
        if (
            handoff is None
            or handoff.eligibility_input_id != delivery.eligibility_input_id
            or handoff.idempotency_key != delivery.idempotency_key
            or handoff.payload_sha256 != delivery.payload_sha256
        ):
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        product = (
            await self.session.execute(
                select(Product).where(
                    Product.tenant_id == tenant_id,
                    Product.id == handoff.product_id,
                    Product.product_code == delivery.payload.product_code,
                )
            )
        ).scalar_one_or_none()
        if product is None:
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        classification = (
            await self.session.execute(
                select(ProductClassificationVersion)
                .where(
                    ProductClassificationVersion.tenant_id == tenant_id,
                    ProductClassificationVersion.product_id == product.id,
                )
                .order_by(ProductClassificationVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if classification is None:
            raise StyleHandoffRepositoryFailure("classification_not_found")
        authorization = None
        if delivery.payload.transform_authorization_id is not None:
            authorization_record = (
                await self.session.execute(
                    select(StyleTransformAuthorizationRecord).where(
                        StyleTransformAuthorizationRecord.tenant_id == tenant_id,
                        StyleTransformAuthorizationRecord.id
                        == delivery.payload.transform_authorization_id,
                    )
                )
            ).scalar_one_or_none()
            if authorization_record is not None:
                authorization = self._authorization_view(authorization_record)
        return StyleAuthoritativeGateState(
            classification_id=classification.id,
            classification_version=classification.version,
            classification_sha256=classification.content_sha256,
            source_image_set_sha256=classification.source_image_set_sha256,
            material_evidence=cast(
                Literal["human_confirmed", "information_insufficient"],
                classification.material_evidence,
            ),
            authorization=authorization,
        )

    async def persist_decision(
        self,
        tenant_id: UUID,
        delivery: StyleWarehouseDeliveryV1,
        *,
        decision: Literal["accepted", "rejected"],
        rejection_code: str | None,
    ) -> StyleWarehouseDecisionReceiptV1:
        existing = await self._decision_by_key(tenant_id, delivery.idempotency_key)
        if existing is not None:
            return self._replay_decision(existing, delivery)
        handoff = (
            await self.session.execute(
                select(StyleWarehouseHandoffRecord)
                .where(
                    StyleWarehouseHandoffRecord.tenant_id == tenant_id,
                    StyleWarehouseHandoffRecord.id == delivery.handoff_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            handoff is None
            or handoff.eligibility_input_id != delivery.eligibility_input_id
            or handoff.idempotency_key != delivery.idempotency_key
            or handoff.payload_sha256 != delivery.payload_sha256
        ):
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        existing = await self._decision_by_key(tenant_id, delivery.idempotency_key)
        if existing is not None:
            return self._replay_decision(existing, delivery)
        decided_at = self.now()
        record = StyleWarehouseDecisionReceiptRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            handoff_id=delivery.handoff_id,
            idempotency_key=delivery.idempotency_key,
            checked_payload_sha256=delivery.payload_sha256,
            decision=decision,
            rejection_code=rejection_code,
            generation_job_id=None,
            payload=delivery.payload.model_dump(mode="json"),
            decided_at=decided_at,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(record)
                await self.session.flush()
        except IntegrityError as error:
            recovered = await self._decision_by_key(tenant_id, delivery.idempotency_key)
            if recovered is None:
                raise StyleHandoffRepositoryFailure("style_handoff_conflict") from error
            return self._replay_decision(recovered, delivery)
        return self._decision_receipt(record)

    async def settle_handoff(
        self,
        tenant_id: UUID,
        handoff_id: UUID,
        receipt: StyleWarehouseDecisionReceiptV1,
    ) -> StoredStyleHandoff:
        row = (
            await self.session.execute(
                select(StyleWarehouseHandoffRecord, StyleEligibilityInputV2Record)
                .join(
                    StyleEligibilityInputV2Record,
                    StyleEligibilityInputV2Record.id
                    == StyleWarehouseHandoffRecord.eligibility_input_id,
                )
                .where(
                    StyleWarehouseHandoffRecord.tenant_id == tenant_id,
                    StyleWarehouseHandoffRecord.id == handoff_id,
                    StyleEligibilityInputV2Record.tenant_id == tenant_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise StyleHandoffRepositoryFailure("style_handoff_not_found")
        handoff, eligibility = row
        if (
            receipt.handoff_id != handoff.id
            or receipt.checked_payload_sha256 != handoff.payload_sha256
        ):
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        if handoff.status in {"accepted", "rejected"}:
            if (
                handoff.decision_receipt_id != receipt.decision_receipt_id
                or handoff.status != receipt.decision
                or handoff.rejection_code != receipt.rejection_code
            ):
                raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        elif handoff.status == "pending":
            handoff.status = receipt.decision
            handoff.decision_receipt_id = receipt.decision_receipt_id
            handoff.rejection_code = receipt.rejection_code
            handoff.settled_at = receipt.decided_at
            await self.session.flush()
        else:
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        product_code = await self.session.scalar(
            select(Product.product_code).where(
                Product.tenant_id == tenant_id,
                Product.id == handoff.product_id,
            )
        )
        if product_code is None:
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        return StoredStyleHandoff(
            eligibility_input_id=eligibility.id,
            payload=StyleEligibilityInputV2.model_validate(eligibility.payload),
            handoff=self._handoff_view(handoff, product_code),
        )

    async def _authorization_by_request(
        self, tenant_id: UUID, authorization_request_id: UUID
    ) -> StyleTransformAuthorizationRecord | None:
        return (
            await self.session.execute(
                select(StyleTransformAuthorizationRecord).where(
                    StyleTransformAuthorizationRecord.tenant_id == tenant_id,
                    StyleTransformAuthorizationRecord.authorization_request_id
                    == authorization_request_id,
                )
            )
        ).scalar_one_or_none()

    async def _decision_by_key(
        self, tenant_id: UUID, idempotency_key: str
    ) -> StyleWarehouseDecisionReceiptRecord | None:
        return (
            await self.session.execute(
                select(StyleWarehouseDecisionReceiptRecord).where(
                    StyleWarehouseDecisionReceiptRecord.tenant_id == tenant_id,
                    StyleWarehouseDecisionReceiptRecord.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()

    async def _input_by_content(
        self, tenant_id: UUID, product_id: UUID, content_sha256: str
    ) -> StyleEligibilityInputV2Record | None:
        return (
            await self.session.execute(
                select(StyleEligibilityInputV2Record).where(
                    StyleEligibilityInputV2Record.tenant_id == tenant_id,
                    StyleEligibilityInputV2Record.product_id == product_id,
                    StyleEligibilityInputV2Record.content_sha256 == content_sha256,
                )
            )
        ).scalar_one_or_none()

    async def _prepared(
        self, record: StyleEligibilityInputV2Record, *, created: bool
    ) -> PreparedStyleEligibility:
        handoff = (
            await self.session.execute(
                select(StyleWarehouseHandoffRecord).where(
                    StyleWarehouseHandoffRecord.tenant_id == record.tenant_id,
                    StyleWarehouseHandoffRecord.eligibility_input_id == record.id,
                )
            )
        ).scalar_one_or_none()
        product_code = await self.session.scalar(
            select(Product.product_code).where(
                Product.tenant_id == record.tenant_id,
                Product.id == record.product_id,
            )
        )
        if handoff is None or product_code is None:
            raise StyleHandoffRepositoryFailure("style_eligibility_conflict")
        return PreparedStyleEligibility(
            eligibility_input_id=record.id,
            payload=StyleEligibilityInputV2.model_validate(record.payload),
            handoff=self._handoff_view(handoff, product_code),
            created=created,
        )

    @staticmethod
    def _replay_authorization(
        record: StyleTransformAuthorizationRecord, request_sha256: str
    ) -> StyleTransformAuthorizationView:
        if record.request_sha256 != request_sha256:
            raise StyleHandoffRepositoryFailure("style_authorization_conflict")
        return StyleHandoffRepository._authorization_view(record)

    @staticmethod
    def _replay_decision(
        record: StyleWarehouseDecisionReceiptRecord,
        delivery: StyleWarehouseDeliveryV1,
    ) -> StyleWarehouseDecisionReceiptV1:
        if (
            record.handoff_id != delivery.handoff_id
            or record.checked_payload_sha256 != delivery.payload_sha256
        ):
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        return StyleHandoffRepository._decision_receipt(record)

    @staticmethod
    def _decision_receipt(
        record: StyleWarehouseDecisionReceiptRecord,
    ) -> StyleWarehouseDecisionReceiptV1:
        if record.decision not in {"accepted", "rejected"}:
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        return StyleWarehouseDecisionReceiptV1(
            decision_receipt_id=record.id,
            handoff_id=record.handoff_id,
            checked_payload_sha256=record.checked_payload_sha256,
            decision=cast(Literal["accepted", "rejected"], record.decision),
            rejection_code=record.rejection_code,
            generation_job_id=None,
            decided_at=record.decided_at,
        )

    @staticmethod
    def _authorization_view(
        record: StyleTransformAuthorizationRecord,
    ) -> StyleTransformAuthorizationView:
        if record.scope != "local_style_acceptance_only":
            raise StyleHandoffRepositoryFailure("style_authorization_conflict")
        return StyleTransformAuthorizationView(
            authorization_id=record.id,
            authorization_request_id=record.authorization_request_id,
            tenant_id=record.tenant_id,
            classification_id=record.classification_id,
            source_image_set_sha256=record.source_image_set_sha256,
            scope="local_style_acceptance_only",
            expires_at=record.expires_at,
            authorized_at=record.authorized_at,
            revoked_at=record.revoked_at,
        )

    @staticmethod
    def _handoff_view(record: StyleWarehouseHandoffRecord, product_code: str) -> StyleHandoffView:
        if record.status not in {"prepared", "pending", "accepted", "rejected"}:
            raise StyleHandoffRepositoryFailure("style_handoff_conflict")
        return StyleHandoffView(
            handoff_id=record.id,
            product_code=product_code,
            payload_version=record.payload_version,
            payload_sha256=record.payload_sha256,
            idempotency_key=record.idempotency_key,
            status=cast(
                Literal["prepared", "pending", "accepted", "rejected"],
                record.status,
            ),
            decision_receipt_id=record.decision_receipt_id,
            rejection_code=record.rejection_code,
            prepared_at=record.prepared_at,
            last_attempted_at=record.last_attempted_at,
            settled_at=record.settled_at,
        )

    @staticmethod
    def _classification_asset(
        asset: ProductClassificationAsset,
        image: SourceImage,
    ) -> StyleClassificationAsset:
        if asset.role not in {"main_candidate", "gallery", "source_original"}:
            raise StyleHandoffRepositoryFailure("classification_ledger_conflict")
        if image.mime_type not in {"image/heic", "image/jpeg", "image/png"}:
            raise StyleHandoffRepositoryFailure("classification_ledger_conflict")
        return StyleClassificationAsset(
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
