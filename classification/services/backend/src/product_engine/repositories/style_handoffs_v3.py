from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.contracts.style_warehouse_v3 import (
    StyleEligibilityInputV3,
    StyleWarehouseDecisionReceiptV2,
    StyleWarehouseDeliveryV2,
    canonical_style_eligibility_v3_sha256,
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
from product_engine.models.style_handoffs import StyleTransformAuthorizationRecord
from product_engine.models.style_handoffs_v3 import (
    StyleEligibilityInputV3Record,
    StyleWarehouseDecisionReceiptV3Record,
    StyleWarehouseHandoffV3Record,
)
from product_engine.repositories.style_handoffs import StyleTransformAuthorizationView


@dataclass(frozen=True)
class StyleV3Asset:
    asset_id: UUID
    source_image_id: UUID
    position: int
    role: Literal["main_candidate", "gallery", "source_original"]
    sha256: str
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]


@dataclass(frozen=True)
class StyleV3Source:
    classification_id: UUID
    tenant_id: UUID
    product_id: UUID
    product_code: str
    version: int
    is_latest: bool
    base_material_package_version: int
    material_label: str
    claim_basis: str | None
    evidence_id: UUID | None
    evidence_content_sha256: str | None
    evidence_active: bool
    product_form: str
    main_source_image_id: UUID
    content_sha256: str
    source_image_set_sha256: str
    assets: tuple[StyleV3Asset, ...]


@dataclass(frozen=True)
class StyleAuthoritativeGateStateV3:
    classification_id: UUID
    classification_version: int
    classification_sha256: str
    source_image_set_sha256: str
    material_claim_basis: (
        Literal["document_supported", "merchant_declared", "information_insufficient"] | None
    )
    material_evidence_id: UUID | None
    material_evidence_content_sha256: str | None
    material_evidence_active: bool
    authorization_id: UUID | None
    authorization_scope: str | None
    authorization_expires_at: datetime | None
    authorization_revoked_at: datetime | None
    authorization_image_set_sha256: str | None


@dataclass(frozen=True)
class PreparedStyleEligibilityV3:
    payload: StyleEligibilityInputV3
    handoff_id: UUID
    created: bool


class StyleHandoffRepositoryV3:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load_latest_source(
        self, tenant_id: UUID, classification_id: UUID
    ) -> StyleV3Source | None:
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
        latest = await self.session.scalar(
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
        evidence = None
        revoked = None
        if classification.material_evidence_id is not None:
            evidence = await self.session.scalar(
                select(MaterialEvidenceRecord).where(
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
        claim_basis = classification.material_claim_basis
        if claim_basis is None and classification.material_evidence == "information_insufficient":
            claim_basis = "information_insufficient"
        return StyleV3Source(
            classification.id,
            tenant_id,
            product.id,
            product.product_code,
            classification.version,
            classification.version == latest,
            classification.base_material_package_version,
            classification.material_label,
            claim_basis,
            classification.material_evidence_id,
            evidence.content_sha256 if evidence is not None else None,
            evidence is not None and revoked is None,
            classification.product_form,
            classification.main_asset_id,
            classification.content_sha256,
            classification.source_image_set_sha256,
            tuple(
                StyleV3Asset(
                    image.upstream_asset_id,
                    image.id,
                    asset.position,
                    cast(Any, asset.role),
                    asset.sha256,
                    cast(Any, image.mime_type),
                )
                for asset, image in asset_rows
            ),
        )

    async def load_latest_authorization(
        self, tenant_id: UUID, classification_id: UUID, source_image_set_sha256: str
    ) -> StyleTransformAuthorizationView | None:
        record = await self.session.scalar(
            select(StyleTransformAuthorizationRecord)
            .where(
                StyleTransformAuthorizationRecord.tenant_id == tenant_id,
                StyleTransformAuthorizationRecord.classification_id == classification_id,
                StyleTransformAuthorizationRecord.source_image_set_sha256
                == source_image_set_sha256,
            )
            .order_by(StyleTransformAuthorizationRecord.authorized_at.desc())
            .limit(1)
        )
        if record is None:
            return None
        return StyleTransformAuthorizationView(
            record.id,
            record.authorization_request_id,
            record.tenant_id,
            record.classification_id,
            record.source_image_set_sha256,
            cast(Any, record.scope),
            record.expires_at,
            record.authorized_at,
            record.revoked_at,
        )

    async def append_eligibility(
        self, candidate: StyleEligibilityInputV3
    ) -> PreparedStyleEligibilityV3:
        existing = await self.session.scalar(
            select(StyleEligibilityInputV3Record).where(
                StyleEligibilityInputV3Record.tenant_id == candidate.tenant_id,
                StyleEligibilityInputV3Record.source_classification_id
                == candidate.source_classification_id,
            )
        )
        if existing is not None:
            handoff_id = await self.session.scalar(
                select(StyleWarehouseHandoffV3Record.id).where(
                    StyleWarehouseHandoffV3Record.eligibility_input_id == existing.id
                )
            )
            return PreparedStyleEligibilityV3(
                StyleEligibilityInputV3.model_validate(existing.payload),
                cast(UUID, handoff_id),
                False,
            )
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
        version = (
            await self.session.scalar(
                select(func.max(StyleEligibilityInputV3Record.version)).where(
                    StyleEligibilityInputV3Record.tenant_id == candidate.tenant_id,
                    StyleEligibilityInputV3Record.product_id == product.id,
                )
            )
            or 0
        ) + 1
        value = candidate.model_copy(
            update={"eligibility_input_version": version, "created_at": datetime.now(UTC)}
        )
        value = value.model_copy(
            update={"content_sha256": canonical_style_eligibility_v3_sha256(value)}
        )
        record = StyleEligibilityInputV3Record(
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
        handoff = StyleWarehouseHandoffV3Record(
            id=uuid4(),
            tenant_id=value.tenant_id,
            product_id=product.id,
            eligibility_input_id=record.id,
            payload_version=version,
            payload_sha256=value.content_sha256,
            idempotency_key=f"style-v3:{value.tenant_id}:{value.source_classification_id}",
            status="prepared",
            prepared_at=value.created_at,
        )
        self.session.add_all([record, handoff])
        await self.session.flush()
        return PreparedStyleEligibilityV3(value, handoff.id, True)

    async def mark_pending_and_build_delivery(
        self, tenant_id: UUID, handoff_id: UUID
    ) -> StyleWarehouseDeliveryV2:
        row = (
            await self.session.execute(
                select(StyleWarehouseHandoffV3Record, StyleEligibilityInputV3Record)
                .join(
                    StyleEligibilityInputV3Record,
                    StyleEligibilityInputV3Record.id
                    == StyleWarehouseHandoffV3Record.eligibility_input_id,
                )
                .where(
                    StyleWarehouseHandoffV3Record.tenant_id == tenant_id,
                    StyleWarehouseHandoffV3Record.id == handoff_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ValueError("style_handoff_not_found")
        handoff, eligibility = row
        handoff.status = "pending"
        handoff.last_attempted_at = datetime.now(UTC)
        return StyleWarehouseDeliveryV2(
            handoff_id=handoff.id,
            idempotency_key=handoff.idempotency_key,
            payload_sha256=handoff.payload_sha256,
            payload=StyleEligibilityInputV3.model_validate(eligibility.payload),
        )

    async def authoritative_gate_state(
        self, tenant_id: UUID, delivery: StyleWarehouseDeliveryV2
    ) -> StyleAuthoritativeGateStateV3:
        source = await self.load_latest_source(tenant_id, delivery.payload.source_classification_id)
        if source is None:
            raise ValueError("classification_not_found")
        authorization = await self.load_latest_authorization(
            tenant_id, source.classification_id, source.source_image_set_sha256
        )
        return StyleAuthoritativeGateStateV3(
            source.classification_id,
            source.version if source.is_latest else -1,
            source.content_sha256,
            source.source_image_set_sha256,
            cast(Any, source.claim_basis),
            source.evidence_id,
            source.evidence_content_sha256,
            source.evidence_active,
            authorization.authorization_id if authorization else None,
            authorization.scope if authorization else None,
            authorization.expires_at if authorization else None,
            authorization.revoked_at if authorization else None,
            authorization.source_image_set_sha256 if authorization else None,
        )

    async def persist_decision(
        self,
        tenant_id: UUID,
        delivery: StyleWarehouseDeliveryV2,
        *,
        decision: Literal["accepted", "rejected"],
        rejection_code: str | None,
    ) -> StyleWarehouseDecisionReceiptV2:
        existing = await self.session.scalar(
            select(StyleWarehouseDecisionReceiptV3Record).where(
                StyleWarehouseDecisionReceiptV3Record.tenant_id == tenant_id,
                StyleWarehouseDecisionReceiptV3Record.idempotency_key == delivery.idempotency_key,
            )
        )
        if existing is not None:
            if existing.checked_payload_sha256 != delivery.payload_sha256:
                raise ValueError("style_handoff_conflict")
            return StyleWarehouseDecisionReceiptV2.model_validate(existing.payload)
        decided_at = datetime.now(UTC)
        receipt = StyleWarehouseDecisionReceiptV2(
            decision_receipt_id=uuid4(),
            handoff_id=delivery.handoff_id,
            checked_payload_sha256=delivery.payload_sha256,
            decision=decision,
            rejection_code=rejection_code,
            generation_job_id=None,
            decided_at=decided_at,
        )
        self.session.add(
            StyleWarehouseDecisionReceiptV3Record(
                id=receipt.decision_receipt_id,
                tenant_id=tenant_id,
                handoff_id=delivery.handoff_id,
                idempotency_key=delivery.idempotency_key,
                checked_payload_sha256=delivery.payload_sha256,
                decision=decision,
                rejection_code=rejection_code,
                generation_job_id=None,
                payload=receipt.model_dump(mode="json"),
                decided_at=decided_at,
            )
        )
        await self.session.flush()
        return receipt
