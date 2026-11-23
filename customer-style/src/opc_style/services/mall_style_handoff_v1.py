"""Fail-closed Mall SaaS intake without generation or publication side effects."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.contracts.mall_style_handoff_v1 import (
    MallStyleAssetRefV1,
    MallStyleHandoffReceiptV1,
    MallStyleHandoffV1,
)
from opc_style.domain.subject_seal import HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION
from opc_style.models import (
    MallStyleHandoffReceipt,
    MallStyleSourceAssetBinding,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductSubjectSeal,
    SceneStyle,
    StyleCardVersion,
    StyleTemplateSlot,
)

_LOCKS_GUARD = Lock()
_LOCKS: dict[tuple[str, str, str], Lock] = defaultdict(Lock)


class MallStyleHandoffConflict(ValueError):
    """One idempotency or source identity was reused with different facts."""


class MallStyleHandoffNotFound(LookupError):
    """No receipt is visible inside the authenticated merchant/opc boundary."""


class MallStyleHandoffRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _handoff_lock(merchant_id: UUID, opc_id: UUID, idempotency_key: str) -> Lock:
    key = (str(merchant_id), str(opc_id), idempotency_key)
    with _LOCKS_GUARD:
        return _LOCKS[key]


class MallStyleHandoffV1Service:
    def __init__(self, session: Session, *, candidate_estimate_usd: Decimal) -> None:
        self.session = session
        self.candidate_estimate_usd = candidate_estimate_usd

    def receive(self, handoff: MallStyleHandoffV1) -> MallStyleHandoffReceiptV1:
        with _handoff_lock(
            handoff.merchant_id, handoff.opc_id, handoff.idempotency_key
        ):
            existing = self._by_key(
                handoff.merchant_id, handoff.opc_id, handoff.idempotency_key
            )
            if existing is not None:
                return self._replay(existing, handoff)
            self._validate_style_selection(handoff)
            self._validate_source_bindings(handoff)
            selected_ref = self._selected_product_ref(handoff)
            product_asset = (
                self._validate_product_asset(handoff, selected_ref)
                if selected_ref is not None
                else None
            )
            subject_seal = (
                self._reviewed_subject(handoff.merchant_id, product_asset)
                if product_asset is not None
                else None
            )
            status = (
                "awaiting_product_photo"
                if product_asset is None
                else (
                    "awaiting_generation_approval"
                    if subject_seal is not None
                    else "awaiting_subject_confirmation"
                )
            )
            now = datetime.now(UTC)
            record = MallStyleHandoffReceipt(
                id=uuid4(),
                handoff_id=handoff.handoff_id,
                merchant_id=handoff.merchant_id,
                opc_id=handoff.opc_id,
                product_entry_submission_id=handoff.product_entry_submission_id,
                product_entry_draft_version=handoff.product_entry_draft_version,
                product_entry_draft_sha256=handoff.product_entry_draft_sha256,
                correlation_id=handoff.correlation_id,
                idempotency_key=handoff.idempotency_key,
                checked_payload_sha256=handoff.payload_sha256,
                status=status,
                product_asset_id=product_asset.id if product_asset else None,
                subject_seal_id=subject_seal.id if subject_seal else None,
                scene_style_id=handoff.style_selection.scene_style_id,
                style_card_version_id=handoff.style_selection.style_card_version_id,
                slot_key=handoff.style_selection.slot_key,
                candidate_count=handoff.candidate_count,
                maximum_cost_usd=(
                    self.candidate_estimate_usd * handoff.candidate_count
                ),
                generation_job_id=None,
                publishable=False,
                payload=handoff.model_dump(mode="json"),
                received_at=now,
                updated_at=now,
            )
            self.session.add(record)
            self.session.add_all(self._new_bindings(handoff))
            try:
                self.session.commit()
            except IntegrityError as error:
                self.session.rollback()
                winner = self._by_key(
                    handoff.merchant_id, handoff.opc_id, handoff.idempotency_key
                )
                if winner is not None:
                    return self._replay(winner, handoff)
                raise MallStyleHandoffConflict("mall_style_source_asset_conflict") from error
            return self._response(record, disposition="accepted")

    def get(
        self, receipt_id: UUID, *, merchant_id: UUID, opc_id: UUID
    ) -> MallStyleHandoffReceiptV1:
        record = self.session.scalar(
            select(MallStyleHandoffReceipt).where(
                MallStyleHandoffReceipt.id == receipt_id,
                MallStyleHandoffReceipt.merchant_id == merchant_id,
                MallStyleHandoffReceipt.opc_id == opc_id,
            )
        )
        if record is None:
            raise MallStyleHandoffNotFound
        self._refresh_status(record)
        return self._response(record, disposition="duplicate")

    def _validate_style_selection(self, handoff: MallStyleHandoffV1) -> None:
        selection = handoff.style_selection
        scene = self.session.scalar(
            select(SceneStyle).where(
                SceneStyle.id == selection.scene_style_id,
                SceneStyle.customer_id == handoff.merchant_id,
                SceneStyle.active.is_(True),
            )
        )
        if scene is None:
            raise MallStyleHandoffRejected("mall_style_selection_unavailable")
        version = self.session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.id == selection.style_card_version_id,
                StyleCardVersion.customer_id == handoff.merchant_id,
                StyleCardVersion.style_card_id == scene.style_card_id,
            )
        )
        latest_version = self.session.scalar(
            select(func.max(StyleCardVersion.version)).where(
                StyleCardVersion.customer_id == handoff.merchant_id,
                StyleCardVersion.style_card_id == scene.style_card_id,
            )
        )
        if version is None or version.version != latest_version:
            raise MallStyleHandoffRejected("mall_style_selection_stale")
        slot = self.session.scalar(
            select(StyleTemplateSlot).where(
                StyleTemplateSlot.customer_id == handoff.merchant_id,
                StyleTemplateSlot.scene_style_id == scene.id,
                StyleTemplateSlot.style_card_version_id == version.id,
                StyleTemplateSlot.slot_key == selection.slot_key,
            )
        )
        if slot is None:
            raise MallStyleHandoffRejected("mall_style_slot_unavailable")

    def _validate_source_bindings(self, handoff: MallStyleHandoffV1) -> None:
        for asset in handoff.assets:
            existing = self.session.scalar(
                select(MallStyleSourceAssetBinding).where(
                    MallStyleSourceAssetBinding.merchant_id == handoff.merchant_id,
                    MallStyleSourceAssetBinding.opc_id == handoff.opc_id,
                    MallStyleSourceAssetBinding.source_asset_id == asset.source_asset_id,
                )
            )
            if existing is not None and (
                existing.source_content_sha256 != asset.source_content_sha256
                or existing.asset_role != asset.asset_role
                or existing.style_product_asset_id != asset.style_product_asset_id
            ):
                raise MallStyleHandoffConflict("mall_style_source_asset_conflict")

    def _new_bindings(
        self, handoff: MallStyleHandoffV1
    ) -> list[MallStyleSourceAssetBinding]:
        rows: list[MallStyleSourceAssetBinding] = []
        for asset in handoff.assets:
            existing = self.session.scalar(
                select(MallStyleSourceAssetBinding.id).where(
                    MallStyleSourceAssetBinding.merchant_id == handoff.merchant_id,
                    MallStyleSourceAssetBinding.opc_id == handoff.opc_id,
                    MallStyleSourceAssetBinding.source_asset_id == asset.source_asset_id,
                )
            )
            if existing is None:
                rows.append(
                    MallStyleSourceAssetBinding(
                        merchant_id=handoff.merchant_id,
                        opc_id=handoff.opc_id,
                        source_asset_id=asset.source_asset_id,
                        source_content_sha256=asset.source_content_sha256,
                        asset_role=asset.asset_role,
                        style_product_asset_id=asset.style_product_asset_id,
                    )
                )
        return rows

    @staticmethod
    def _selected_product_ref(
        handoff: MallStyleHandoffV1,
    ) -> MallStyleAssetRefV1 | None:
        if handoff.selected_product_photo_source_asset_id is None:
            return None
        return next(
            asset
            for asset in handoff.assets
            if asset.source_asset_id == handoff.selected_product_photo_source_asset_id
        )

    def _validate_product_asset(
        self, handoff: MallStyleHandoffV1, source: MallStyleAssetRefV1
    ) -> ProductAsset:
        asset = self.session.scalar(
            select(ProductAsset).where(
                ProductAsset.id == source.style_product_asset_id,
                ProductAsset.customer_id == handoff.merchant_id,
                ProductAsset.product_id == handoff.product_id,
                ProductAsset.content_sha256 == source.source_content_sha256,
                ProductAsset.confirmed.is_(True),
                ProductAsset.identity == "original_real_photo",
            )
        )
        if asset is None:
            raise MallStyleHandoffRejected("mall_product_asset_unavailable")
        permission = self._latest_permission(handoff.merchant_id, asset.id)
        if permission is None or not permission.transform_permission:
            raise MallStyleHandoffRejected("mall_product_asset_not_authorized")
        return asset

    def _latest_permission(
        self, merchant_id: UUID, asset_id: UUID
    ) -> ProductAssetPermissionEvent | None:
        return self.session.scalar(
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

    def _reviewed_subject(
        self, merchant_id: UUID, asset: ProductAsset
    ) -> ProductSubjectSeal | None:
        permission = self._latest_permission(merchant_id, asset.id)
        if permission is None or not permission.transform_permission:
            return None
        return self.session.scalar(
            select(ProductSubjectSeal)
            .where(
                ProductSubjectSeal.customer_id == merchant_id,
                ProductSubjectSeal.product_asset_id == asset.id,
                ProductSubjectSeal.source_sha256 == asset.content_sha256,
                ProductSubjectSeal.transform_permission_event_id == permission.id,
                ProductSubjectSeal.segmentation_adapter_version
                == HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION,
                ProductSubjectSeal.publishable.is_(False),
            )
            .order_by(ProductSubjectSeal.created_at.desc(), ProductSubjectSeal.id.desc())
            .limit(1)
        )

    def _refresh_status(self, record: MallStyleHandoffReceipt) -> None:
        if record.product_asset_id is None:
            return
        asset = self.session.scalar(
            select(ProductAsset).where(
                ProductAsset.id == record.product_asset_id,
                ProductAsset.customer_id == record.merchant_id,
            )
        )
        seal = self._reviewed_subject(record.merchant_id, asset) if asset else None
        next_status = (
            "awaiting_generation_approval"
            if seal is not None
            else "awaiting_subject_confirmation"
        )
        if next_status != record.status or record.subject_seal_id != (
            seal.id if seal else None
        ):
            record.status = next_status
            record.subject_seal_id = seal.id if seal else None
            record.updated_at = datetime.now(UTC)
            self.session.commit()

    def _by_key(
        self, merchant_id: UUID, opc_id: UUID, idempotency_key: str
    ) -> MallStyleHandoffReceipt | None:
        return self.session.scalar(
            select(MallStyleHandoffReceipt).where(
                MallStyleHandoffReceipt.merchant_id == merchant_id,
                MallStyleHandoffReceipt.opc_id == opc_id,
                MallStyleHandoffReceipt.idempotency_key == idempotency_key,
            )
        )

    def _replay(
        self, record: MallStyleHandoffReceipt, handoff: MallStyleHandoffV1
    ) -> MallStyleHandoffReceiptV1:
        if (
            record.checked_payload_sha256 != handoff.payload_sha256
            or record.handoff_id != handoff.handoff_id
            or record.opc_id != handoff.opc_id
        ):
            raise MallStyleHandoffConflict("mall_style_handoff_conflict")
        self._refresh_status(record)
        return self._response(record, disposition="duplicate")

    @staticmethod
    def _response(
        record: MallStyleHandoffReceipt,
        *,
        disposition: Literal["accepted", "duplicate"],
    ) -> MallStyleHandoffReceiptV1:
        return MallStyleHandoffReceiptV1.model_validate(
            {
                "receipt_id": record.id,
                "handoff_id": record.handoff_id,
                "merchant_id": record.merchant_id,
                "opc_id": record.opc_id,
                "product_entry_submission_id": record.product_entry_submission_id,
                "product_entry_draft_version": record.product_entry_draft_version,
                "product_entry_draft_sha256": record.product_entry_draft_sha256,
                "correlation_id": record.correlation_id,
                "checked_payload_sha256": record.checked_payload_sha256,
                "status": record.status,
                "product_asset_id": record.product_asset_id,
                "subject_seal_id": record.subject_seal_id,
                "scene_style_id": record.scene_style_id,
                "style_card_version_id": record.style_card_version_id,
                "slot_key": record.slot_key,
                "candidate_count": record.candidate_count,
                "maximum_cost_usd": record.maximum_cost_usd,
                "approval_required": True,
                "generation_job_id": None,
                "publishable": False,
                "disposition": disposition,
                "received_at": _utc(record.received_at),
                "updated_at": _utc(record.updated_at),
            }
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
