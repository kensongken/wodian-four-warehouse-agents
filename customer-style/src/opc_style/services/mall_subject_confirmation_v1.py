"""Mall owner bridge to the existing human-reviewed ProductSubjectSeal flow."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from threading import Lock
from typing import Literal, cast
from uuid import UUID, uuid4

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.contracts.mall_style_handoff_v1 import (
    MallSubjectConfirmationMetadataV1,
    MallSubjectConfirmationReceiptV1,
    MallSubjectInspectionV1,
    canonical_mall_subject_confirmation_signature_bytes,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.subject_seal import (
    HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION,
    PRODUCT_SUBJECT_CANVAS,
    SealedSubject,
    SubjectSealError,
    SubjectSegmentationRequest,
    SubjectSegmenter,
    seal_subject_with_mask,
)
from opc_style.models import (
    AuditEvent,
    MallStyleHandoffReceipt,
    MallStyleSourceAssetBinding,
    MallStyleSubjectConfirmation,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductSubjectSeal,
)
from opc_style.services.real_photos import AssetContentStoreError
from opc_style.services.subject_seals import (
    ProductSubjectSealNotFoundError,
    ProductSubjectSealService,
    SubjectSealContentStore,
)

_LOCKS_GUARD = Lock()
_LOCKS: dict[tuple[str, str, str], Lock] = defaultdict(Lock)


MANUAL_FULL_FRAME_ADAPTER_VERSION = "manual-full-frame-v1"


class ManualFullFrameSubjectProposal:
    """Expose actual image pixels for owner mask editing; never pretend to segment."""

    adapter_version = MANUAL_FULL_FRAME_ADAPTER_VERSION
    synthetic_test_only = False

    def seal(self, request: SubjectSegmentationRequest) -> SealedSubject:
        with Image.open(BytesIO(request.content)) as opened:
            source = ImageOps.exif_transpose(opened)
            mask = Image.new("L", source.size, 255)
        output = BytesIO()
        mask.save(output, format="PNG")
        return seal_subject_with_mask(
            request,
            output.getvalue(),
            adapter_version=self.adapter_version,
            synthetic_test_only=False,
        )


class MallSubjectNotFound(LookupError):
    """Conceal missing or foreign Mall subject lineage."""


class MallSubjectConflict(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _lock(merchant_id: UUID, opc_id: UUID, receipt_id: UUID) -> Lock:
    key = (str(merchant_id), str(opc_id), str(receipt_id))
    with _LOCKS_GUARD:
        return _LOCKS[key]


class MallSubjectConfirmationService:
    def __init__(self, session: Session, store: SubjectSealContentStore) -> None:
        self._session = session
        self._store = store

    def inspect(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        receipt_id: UUID,
    ) -> MallSubjectInspectionV1:
        receipt, asset = self._lineage(merchant_id, opc_id, product_id, receipt_id)
        confirmation = self._confirmation(merchant_id, opc_id, receipt.id)
        if confirmation is not None:
            reviewed = self._session.scalar(
                select(ProductSubjectSeal).where(
                    ProductSubjectSeal.id == confirmation.reviewed_subject_seal_id,
                    ProductSubjectSeal.customer_id == merchant_id,
                )
            )
            base = self._session.scalar(
                select(ProductSubjectSeal).where(
                    ProductSubjectSeal.id == confirmation.base_subject_seal_id,
                    ProductSubjectSeal.customer_id == merchant_id,
                )
            )
            if (
                reviewed is None
                or base is None
                or not self._authority_current(asset, base)
                or not self._authority_current(asset, reviewed)
                or base.mask_sha256 != confirmation.base_mask_sha256
                or reviewed.product_asset_id != asset.id
                or reviewed.segmentation_adapter_version
                != HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION
            ):
                raise MallSubjectConflict("mall_subject_confirmation_invalidated")
            return self._inspection(receipt, asset, base, confirmation=confirmation)
        base = self._current_base(asset)
        if base is None:
            return self._inspection(receipt, asset, None)
        return self._inspection(receipt, asset, base)

    def propose(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        receipt_id: UUID,
        segmenter: SubjectSegmenter,
    ) -> MallSubjectInspectionV1:
        receipt, asset = self._lineage(merchant_id, opc_id, product_id, receipt_id)
        existing = self._current_base(asset)
        if existing is None:
            try:
                output = ProductSubjectSealService(self._session, self._store).seal(
                    merchant_id,
                    asset_id=asset.id,
                    segmenter=segmenter,
                    canvas_size=PRODUCT_SUBJECT_CANVAS,
                )
            except (DomainRuleError, SubjectSealError) as error:
                raise MallSubjectConflict(error.code) from error
            except (ProductSubjectSealNotFoundError, AssetContentStoreError) as error:
                raise MallSubjectNotFound from error
            existing = output.seal
        return self._inspection(receipt, asset, existing)

    def confirm(
        self,
        metadata: MallSubjectConfirmationMetadataV1,
        reviewed_mask_png: bytes,
        *,
        merchant_id: UUID,
        opc_id: UUID,
    ) -> MallSubjectConfirmationReceiptV1:
        with _lock(merchant_id, opc_id, metadata.receipt_id):
            existing = self._confirmation(merchant_id, opc_id, metadata.receipt_id)
            payload_sha256 = sha256(
                canonical_mall_subject_confirmation_signature_bytes(
                    metadata, reviewed_mask_png
                )
            ).hexdigest()
            if existing is not None:
                self.inspect(
                    merchant_id=merchant_id,
                    opc_id=opc_id,
                    product_id=metadata.product_id,
                    receipt_id=metadata.receipt_id,
                )
                return self._replay(existing, metadata, payload_sha256)
            receipt, asset = self._lineage(
                merchant_id, opc_id, metadata.product_id, metadata.receipt_id
            )
            if metadata.product_asset_sha256 != asset.content_sha256:
                raise MallSubjectConflict("mall_subject_asset_digest_mismatch")
            if sha256(reviewed_mask_png).hexdigest() != metadata.reviewed_mask_sha256:
                raise MallSubjectConflict("mall_subject_reviewed_mask_digest_mismatch")
            base = self._session.scalar(
                select(ProductSubjectSeal).where(
                    ProductSubjectSeal.id == metadata.base_subject_seal_id,
                    ProductSubjectSeal.customer_id == merchant_id,
                    ProductSubjectSeal.product_asset_id == asset.id,
                    ProductSubjectSeal.source_sha256 == asset.content_sha256,
                )
            )
            if (
                base is None
                or base.mask_sha256 != metadata.base_mask_sha256
                or not self._authority_current(asset, base)
            ):
                raise MallSubjectConflict("mall_subject_proposal_invalidated")
            try:
                reviewed = (
                    ProductSubjectSealService(self._session, self._store)
                    .confirm_reviewed_mask(
                        merchant_id,
                        base_seal_id=base.id,
                        reviewed_mask_png=reviewed_mask_png,
                        confirmation_key=metadata.idempotency_key,
                        manual_selection=(
                            base.segmentation_adapter_version
                            == MANUAL_FULL_FRAME_ADAPTER_VERSION
                        ),
                    )
                    .seal
                )
            except (DomainRuleError, SubjectSealError) as error:
                raise MallSubjectConflict(error.code) from error
            except (ProductSubjectSealNotFoundError, AssetContentStoreError) as error:
                raise MallSubjectNotFound from error
            audit = AuditEvent(
                id=uuid4(),
                customer_id=merchant_id,
                event_type="mall_product_subject.owner_confirmed",
                payload={
                    "receipt_id": str(receipt.id),
                    "product_id": asset.product_id,
                    "product_asset_id": str(asset.id),
                    "source_sha256": asset.content_sha256,
                    "base_subject_seal_id": str(base.id),
                    "base_mask_sha256": base.mask_sha256,
                    "proposal_version": metadata.proposal_version,
                    "reviewed_mask_sha256": metadata.reviewed_mask_sha256,
                    "reviewed_subject_seal_id": str(reviewed.id),
                    "owner_actor_reference": metadata.owner_actor_reference,
                    "actor_role": metadata.actor_role,
                    "publishable": False,
                },
                occurred_at=datetime.now(UTC),
            )
            row = MallStyleSubjectConfirmation(
                id=uuid4(),
                receipt_id=receipt.id,
                merchant_id=merchant_id,
                opc_id=opc_id,
                product_id=asset.product_id,
                product_asset_id=asset.id,
                source_sha256=asset.content_sha256,
                base_subject_seal_id=base.id,
                base_mask_sha256=base.mask_sha256,
                proposal_version=metadata.proposal_version,
                reviewed_mask_sha256=metadata.reviewed_mask_sha256,
                reviewed_subject_seal_id=reviewed.id,
                owner_actor_reference=metadata.owner_actor_reference,
                actor_role=metadata.actor_role,
                idempotency_key=metadata.idempotency_key,
                confirmation_payload_sha256=payload_sha256,
                audit_event_id=audit.id,
                publishable=False,
                created_at=datetime.now(UTC),
            )
            self._session.add_all([audit, row])
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                winner = self._confirmation(merchant_id, opc_id, receipt.id)
                if winner is None:
                    raise
                return self._replay(winner, metadata, payload_sha256)
            return _confirmation_response(row, disposition="accepted")

    def read_original(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        receipt_id: UUID,
    ) -> tuple[bytes, str, str]:
        _, asset = self._lineage(merchant_id, opc_id, product_id, receipt_id)
        try:
            content = self._store.read_original(
                merchant_id, asset.id, asset.content_sha256
            )
        except AssetContentStoreError as error:
            raise MallSubjectNotFound from error
        return content, asset.mime_type, asset.content_sha256

    def read_proposal_artifact(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        receipt_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
    ) -> tuple[bytes, str]:
        _, asset = self._lineage(merchant_id, opc_id, product_id, receipt_id)
        seal = self._session.scalar(
            select(ProductSubjectSeal).where(
                ProductSubjectSeal.id == seal_id,
                ProductSubjectSeal.customer_id == merchant_id,
                ProductSubjectSeal.product_asset_id == asset.id,
                ProductSubjectSeal.source_sha256 == asset.content_sha256,
            )
        )
        if seal is None or not self._authority_current(asset, seal):
            raise MallSubjectNotFound
        digest = seal.normalized_subject_sha256 if kind == "rgba" else seal.mask_sha256
        try:
            content = self._store.read_subject_artifact(
                merchant_id, seal.id, kind, digest
            )
        except AssetContentStoreError as error:
            raise MallSubjectNotFound from error
        return content, digest

    def _lineage(
        self, merchant_id: UUID, opc_id: UUID, product_id: str, receipt_id: UUID
    ) -> tuple[MallStyleHandoffReceipt, ProductAsset]:
        receipt = self._session.scalar(
            select(MallStyleHandoffReceipt).where(
                MallStyleHandoffReceipt.id == receipt_id,
                MallStyleHandoffReceipt.merchant_id == merchant_id,
                MallStyleHandoffReceipt.opc_id == opc_id,
                MallStyleHandoffReceipt.product_asset_id.is_not(None),
            )
        )
        if receipt is None or receipt.payload.get("product_id") != product_id:
            raise MallSubjectNotFound
        asset = self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.id == receipt.product_asset_id,
                ProductAsset.customer_id == merchant_id,
                ProductAsset.product_id == product_id,
                ProductAsset.identity == "original_real_photo",
                ProductAsset.confirmed.is_(True),
            )
        )
        binding = self._session.scalar(
            select(MallStyleSourceAssetBinding).where(
                MallStyleSourceAssetBinding.merchant_id == merchant_id,
                MallStyleSourceAssetBinding.opc_id == opc_id,
                MallStyleSourceAssetBinding.style_product_asset_id
                == receipt.product_asset_id,
                MallStyleSourceAssetBinding.asset_role == "product_photo",
            )
        )
        if (
            asset is None
            or binding is None
            or binding.source_content_sha256 != asset.content_sha256
        ):
            raise MallSubjectNotFound
        permission = _latest_permission(self._session, merchant_id, asset.id)
        if permission is None or not permission.transform_permission:
            raise MallSubjectNotFound
        return receipt, asset

    def _current_base(self, asset: ProductAsset) -> ProductSubjectSeal | None:
        permission = _latest_permission(self._session, asset.customer_id, asset.id)
        if permission is None or not permission.transform_permission:
            raise MallSubjectConflict("mall_subject_rights_unavailable")
        return self._session.scalar(
            select(ProductSubjectSeal)
            .where(
                ProductSubjectSeal.customer_id == asset.customer_id,
                ProductSubjectSeal.product_asset_id == asset.id,
                ProductSubjectSeal.source_sha256 == asset.content_sha256,
                ProductSubjectSeal.transform_permission_event_id == permission.id,
                ProductSubjectSeal.publishable.is_(False),
            )
            .order_by(
                (
                    ProductSubjectSeal.segmentation_adapter_version
                    == HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION
                ).desc(),
                ProductSubjectSeal.created_at.desc(),
                ProductSubjectSeal.id.desc(),
            )
            .limit(1)
        )

    def _authority_current(self, asset: ProductAsset, seal: ProductSubjectSeal) -> bool:
        permission = _latest_permission(self._session, asset.customer_id, asset.id)
        return (
            permission is not None
            and permission.transform_permission
            and seal.transform_permission_event_id == permission.id
            and seal.source_sha256 == asset.content_sha256
            and seal.publishable is False
        )

    def _inspection(
        self,
        receipt: MallStyleHandoffReceipt,
        asset: ProductAsset,
        base: ProductSubjectSeal | None,
        *,
        confirmation: MallStyleSubjectConfirmation | None = None,
    ) -> MallSubjectInspectionV1:
        prefix = (
            f"/internal/mall-style/v1/products/{asset.product_id}/receipts/{receipt.id}"
        )
        reviewed = (
            base is not None
            and base.segmentation_adapter_version
            == HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION
        )
        status: Literal["proposal_required", "needs_human_review", "confirmed"] = (
            "proposal_required"
            if base is None
            else "confirmed"
            if confirmation is not None
            else "needs_human_review"
        )
        return MallSubjectInspectionV1(
            merchant_id=receipt.merchant_id,
            opc_id=receipt.opc_id,
            receipt_id=receipt.id,
            product_id=asset.product_id,
            product_asset_id=asset.id,
            product_asset_sha256=asset.content_sha256,
            status=status,
            proposal_method=(
                "manual_full_frame"
                if base
                and base.segmentation_adapter_version
                == MANUAL_FULL_FRAME_ADAPTER_VERSION
                else "automatic"
                if base
                else None
            ),
            manual_confirmation_required=confirmation is None,
            quality_warning=(
                "当前为原图人工框选，尚未自动去除背景。请店主擦除背景并保留完整商品；框选不准确会影响商品图质量。"
                if base
                and base.segmentation_adapter_version
                == MANUAL_FULL_FRAME_ADAPTER_VERSION
                else None
            ),
            base_subject_seal_id=base.id if base else None,
            proposal_version=1 if base else None,
            base_mask_sha256=base.mask_sha256 if base else None,
            normalized_subject_sha256=(
                base.normalized_subject_sha256 if base else None
            ),
            width=base.normalized_width if base else None,
            height=base.normalized_height if base else None,
            original_url=f"{prefix}/subject-assets/original",
            rgba_url=f"{prefix}/subject-proposals/{base.id}/rgba" if base else None,
            mask_url=f"{prefix}/subject-proposals/{base.id}/mask" if base else None,
            reviewed_subject_seal_id=(
                confirmation.reviewed_subject_seal_id
                if confirmation
                else base.id
                if reviewed and base
                else None
            ),
            owner_actor_reference=(
                confirmation.owner_actor_reference if confirmation else None
            ),
            audit_event_id=confirmation.audit_event_id if confirmation else None,
            publishable=False,
        )

    def _confirmation(
        self, merchant_id: UUID, opc_id: UUID, receipt_id: UUID
    ) -> MallStyleSubjectConfirmation | None:
        return self._session.scalar(
            select(MallStyleSubjectConfirmation).where(
                MallStyleSubjectConfirmation.merchant_id == merchant_id,
                MallStyleSubjectConfirmation.opc_id == opc_id,
                MallStyleSubjectConfirmation.receipt_id == receipt_id,
            )
        )

    @staticmethod
    def _replay(
        row: MallStyleSubjectConfirmation,
        metadata: MallSubjectConfirmationMetadataV1,
        payload_sha256: str,
    ) -> MallSubjectConfirmationReceiptV1:
        if (
            row.confirmation_payload_sha256 != payload_sha256
            or row.idempotency_key != metadata.idempotency_key
            or row.owner_actor_reference != metadata.owner_actor_reference
            or row.reviewed_mask_sha256 != metadata.reviewed_mask_sha256
        ):
            raise MallSubjectConflict("mall_subject_confirmation_conflict")
        return _confirmation_response(row, disposition="duplicate")


def _latest_permission(
    session: Session, merchant_id: UUID, asset_id: UUID
) -> ProductAssetPermissionEvent | None:
    return session.scalar(
        select(ProductAssetPermissionEvent)
        .where(
            ProductAssetPermissionEvent.customer_id == merchant_id,
            ProductAssetPermissionEvent.product_asset_id == asset_id,
        )
        .order_by(
            ProductAssetPermissionEvent.occurred_at.desc(),
            ProductAssetPermissionEvent.created_at.desc(),
            ProductAssetPermissionEvent.id.desc(),
        )
        .limit(1)
    )


def _confirmation_response(
    row: MallStyleSubjectConfirmation,
    *,
    disposition: Literal["accepted", "duplicate"],
) -> MallSubjectConfirmationReceiptV1:
    return MallSubjectConfirmationReceiptV1(
        confirmation_id=row.id,
        merchant_id=row.merchant_id,
        opc_id=row.opc_id,
        receipt_id=row.receipt_id,
        product_id=row.product_id,
        product_asset_id=row.product_asset_id,
        product_asset_sha256=row.source_sha256,
        base_subject_seal_id=row.base_subject_seal_id,
        base_mask_sha256=row.base_mask_sha256,
        proposal_version=cast(Literal[1], row.proposal_version),
        reviewed_mask_sha256=row.reviewed_mask_sha256,
        reviewed_subject_seal_id=row.reviewed_subject_seal_id,
        owner_actor_reference=row.owner_actor_reference,
        actor_role="merchant_owner",
        audit_event_id=row.audit_event_id,
        disposition=disposition,
        publishable=False,
        created_at=(
            row.created_at.replace(tzinfo=UTC)
            if row.created_at.tzinfo is None
            else row.created_at.astimezone(UTC)
        ),
    )
