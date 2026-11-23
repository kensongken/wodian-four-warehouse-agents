"""Mall fee approval queue plus tenant-bound, non-publishable result projection."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Lock
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.contracts.mall_style_handoff_v1 import (
    MallStyleGenerationApprovalV1,
    MallStyleGenerationQueueReceiptV1,
    MallStyleResultCandidateV1,
    MallStyleResultListV1,
)
from opc_style.models import (
    AuditEvent,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    MallStyleGenerationQueueItem,
    MallStyleHandoffReceipt,
    MallStyleSourceAssetBinding,
    MallStyleSubjectConfirmation,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductSubjectSeal,
)
from opc_style.services.mall_style_handoff_v1 import MallStyleHandoffV1Service
from opc_style.services.mall_style_integration_v1 import MallStyleSelectionService
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError

_LOCKS_GUARD = Lock()
_LOCKS: dict[tuple[str, str, str], Lock] = defaultdict(Lock)


class MallStyleResultNotFound(LookupError):
    """No matching receipt, request, job or candidate exists in this tenant."""


class MallStyleGenerationApprovalConflict(ValueError):
    """The approval key or receipt was reused with different immutable facts."""


class MallStyleGenerationApprovalRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _approval_lock(merchant_id: UUID, opc_id: UUID, receipt_id: UUID) -> Lock:
    key = (str(merchant_id), str(opc_id), str(receipt_id))
    with _LOCKS_GUARD:
        return _LOCKS[key]


class MallStyleGenerationApprovalService:
    """Persist a safe pending request; never call a generation provider."""

    def __init__(self, session: Session, *, candidate_estimate_usd: Decimal) -> None:
        self._session = session
        self._candidate_estimate_usd = candidate_estimate_usd

    def approve(
        self,
        approval: MallStyleGenerationApprovalV1,
        *,
        merchant_id: UUID,
        opc_id: UUID,
    ) -> MallStyleGenerationQueueReceiptV1:
        now = datetime.now(UTC)
        with _approval_lock(merchant_id, opc_id, approval.receipt_id):
            existing = self._by_receipt(merchant_id, opc_id, approval.receipt_id)
            if existing is not None:
                receipt = self._load_receipt(
                    approval.receipt_id, merchant_id=merchant_id, opc_id=opc_id
                )
                failure = MallStyleResultService(self._session)._authority_failure(
                    existing, receipt
                )
                if failure is not None:
                    raise MallStyleGenerationApprovalRejected(failure)
                if _utc(existing.approval_expires_at) <= now:
                    raise MallStyleGenerationApprovalRejected(
                        "mall_style_approval_expired"
                    )
                return self._replay(existing, approval)
            if approval.approved_at > now + timedelta(minutes=5):
                raise MallStyleGenerationApprovalRejected(
                    "mall_style_approval_time_invalid"
                )
            if approval.actor_role != "merchant_owner":
                raise MallStyleGenerationApprovalRejected("mall_style_owner_required")
            if approval.expires_at - approval.approved_at > timedelta(hours=24):
                raise MallStyleGenerationApprovalRejected(
                    "mall_style_approval_validity_too_long"
                )
            if approval.expires_at <= now:
                raise MallStyleGenerationApprovalRejected("mall_style_approval_expired")
            receipt = self._load_receipt(
                approval.receipt_id, merchant_id=merchant_id, opc_id=opc_id
            )
            MallStyleHandoffV1Service(
                self._session, candidate_estimate_usd=self._candidate_estimate_usd
            ).get(receipt.id, merchant_id=merchant_id, opc_id=opc_id)
            self._session.refresh(receipt)
            product_id = _receipt_product_id(receipt)
            if approval.product_id != product_id:
                raise MallStyleGenerationApprovalRejected(
                    "mall_style_approval_product_mismatch"
                )
            if receipt.status != "awaiting_generation_approval":
                raise MallStyleGenerationApprovalRejected(
                    "mall_style_generation_not_ready"
                )
            if receipt.product_asset_id is None or receipt.subject_seal_id is None:
                raise MallStyleGenerationApprovalRejected(
                    "mall_style_generation_not_ready"
                )
            if approval.approved_candidate_count != receipt.candidate_count:
                raise MallStyleGenerationApprovalRejected(
                    "mall_style_approval_candidate_count_mismatch"
                )
            if approval.approved_maximum_cost_usd != receipt.maximum_cost_usd:
                raise MallStyleGenerationApprovalRejected(
                    "mall_style_approval_cost_mismatch"
                )
            asset, permission, seal = self._current_rights(receipt, product_id)
            if not self._style_available(receipt):
                raise MallStyleGenerationApprovalRejected(
                    "mall_style_selection_unavailable"
                )
            confirmation = self._session.scalar(
                select(MallStyleSubjectConfirmation).where(
                    MallStyleSubjectConfirmation.receipt_id == receipt.id,
                    MallStyleSubjectConfirmation.merchant_id == merchant_id,
                    MallStyleSubjectConfirmation.opc_id == opc_id,
                    MallStyleSubjectConfirmation.reviewed_subject_seal_id == seal.id,
                    MallStyleSubjectConfirmation.source_sha256 == asset.content_sha256,
                )
            )
            if confirmation is None:
                raise MallStyleGenerationApprovalRejected(
                    "mall_subject_owner_confirmation_required"
                )
            audit = AuditEvent(
                id=uuid4(),
                customer_id=merchant_id,
                event_type="mall_style_generation.owner_approved",
                occurred_at=now,
                payload={
                    "receipt_id": str(receipt.id),
                    "provider": approval.provider,
                    "model": approval.model,
                    "owner_actor_reference": approval.owner_actor_reference,
                    "actor_role": approval.actor_role,
                    "approval_payload_sha256": approval.approval_payload_sha256,
                    "candidate_count": approval.approved_candidate_count,
                    "maximum_cost_usd": str(approval.approved_maximum_cost_usd),
                    "approval_expires_at": approval.expires_at.isoformat(),
                    "automatic_retry_allowed": False,
                    "publishable": False,
                },
            )
            row = MallStyleGenerationQueueItem(
                id=uuid4(),
                receipt_id=receipt.id,
                merchant_id=merchant_id,
                opc_id=opc_id,
                product_id=product_id,
                idempotency_key=approval.idempotency_key,
                approval_reference=approval.approval_reference,
                provider=approval.provider,
                model=approval.model,
                owner_actor_reference=approval.owner_actor_reference,
                actor_role=approval.actor_role,
                approval_audit_event_id=audit.id,
                approval_payload_sha256=approval.approval_payload_sha256,
                approval_expires_at=approval.expires_at,
                rights_permission_event_id=permission.id,
                rights_reference=f"product_asset_permission_event:{permission.id}",
                product_asset_id=asset.id,
                subject_seal_id=seal.id,
                scene_style_id=receipt.scene_style_id,
                style_card_version_id=receipt.style_card_version_id,
                slot_key=receipt.slot_key,
                candidate_count=approval.approved_candidate_count,
                maximum_cost_usd=approval.approved_maximum_cost_usd,
                status="pending_execution",
                generation_job_id=None,
                failure_reason=None,
                publishable=False,
                created_at=now,
                updated_at=now,
            )
            self._session.add_all([row, audit])
            try:
                self._session.commit()
            except IntegrityError as error:
                self._session.rollback()
                winner = self._by_receipt(merchant_id, opc_id, approval.receipt_id)
                if winner is None:
                    raise MallStyleGenerationApprovalConflict(
                        "mall_style_generation_approval_conflict"
                    ) from error
                return self._replay(winner, approval)
            return _queue_response(row, disposition="accepted")

    def _load_receipt(
        self, receipt_id: UUID, *, merchant_id: UUID, opc_id: UUID
    ) -> MallStyleHandoffReceipt:
        receipt = self._session.scalar(
            select(MallStyleHandoffReceipt).where(
                MallStyleHandoffReceipt.id == receipt_id,
                MallStyleHandoffReceipt.merchant_id == merchant_id,
                MallStyleHandoffReceipt.opc_id == opc_id,
            )
        )
        if receipt is None:
            raise MallStyleResultNotFound
        return receipt

    def _current_rights(
        self, receipt: MallStyleHandoffReceipt, product_id: str
    ) -> tuple[ProductAsset, ProductAssetPermissionEvent, ProductSubjectSeal]:
        assert receipt.product_asset_id is not None
        assert receipt.subject_seal_id is not None
        asset = self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.id == receipt.product_asset_id,
                ProductAsset.customer_id == receipt.merchant_id,
                ProductAsset.product_id == product_id,
                ProductAsset.identity == "original_real_photo",
                ProductAsset.confirmed.is_(True),
            )
        )
        binding = self._session.scalar(
            select(MallStyleSourceAssetBinding).where(
                MallStyleSourceAssetBinding.merchant_id == receipt.merchant_id,
                MallStyleSourceAssetBinding.opc_id == receipt.opc_id,
                MallStyleSourceAssetBinding.style_product_asset_id
                == receipt.product_asset_id,
                MallStyleSourceAssetBinding.asset_role == "product_photo",
            )
        )
        if asset is None or binding is None:
            raise MallStyleGenerationApprovalRejected(
                "mall_style_product_asset_unavailable"
            )
        permission = _latest_permission(self._session, receipt.merchant_id, asset.id)
        seal = self._session.scalar(
            select(ProductSubjectSeal).where(
                ProductSubjectSeal.id == receipt.subject_seal_id,
                ProductSubjectSeal.customer_id == receipt.merchant_id,
                ProductSubjectSeal.product_asset_id == asset.id,
                ProductSubjectSeal.source_sha256 == asset.content_sha256,
                ProductSubjectSeal.publishable.is_(False),
            )
        )
        if (
            binding.source_content_sha256 != asset.content_sha256
            or permission is None
            or not permission.transform_permission
            or seal is None
            or seal.transform_permission_event_id != permission.id
        ):
            raise MallStyleGenerationApprovalRejected("mall_style_rights_unavailable")
        return asset, permission, seal

    def _style_available(self, receipt: MallStyleHandoffReceipt) -> bool:
        catalogue = MallStyleSelectionService(self._session).list_available(
            merchant_id=receipt.merchant_id, opc_id=receipt.opc_id
        )
        return any(
            row.scene_style_id == receipt.scene_style_id
            and row.style_card_version_id == receipt.style_card_version_id
            and row.slot_key == receipt.slot_key
            for row in catalogue.selections
        )

    def _by_receipt(
        self, merchant_id: UUID, opc_id: UUID, receipt_id: UUID
    ) -> MallStyleGenerationQueueItem | None:
        return self._session.scalar(
            select(MallStyleGenerationQueueItem).where(
                MallStyleGenerationQueueItem.merchant_id == merchant_id,
                MallStyleGenerationQueueItem.opc_id == opc_id,
                MallStyleGenerationQueueItem.receipt_id == receipt_id,
            )
        )

    @staticmethod
    def _replay(
        row: MallStyleGenerationQueueItem,
        approval: MallStyleGenerationApprovalV1,
    ) -> MallStyleGenerationQueueReceiptV1:
        if (
            row.approval_payload_sha256 != approval.approval_payload_sha256
            or row.idempotency_key != approval.idempotency_key
            or row.approval_reference != approval.approval_reference
            or row.product_id != approval.product_id
        ):
            raise MallStyleGenerationApprovalConflict(
                "mall_style_generation_approval_conflict"
            )
        return _queue_response(row, disposition="duplicate")


class MallStyleResultService:
    """Project one exact receipt/job lineage without exposing storage paths."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def by_receipt(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        receipt_id: UUID,
    ) -> MallStyleResultListV1:
        receipt = self._receipt(merchant_id, opc_id, product_id, receipt_id)
        queue = self._session.scalar(
            select(MallStyleGenerationQueueItem).where(
                MallStyleGenerationQueueItem.merchant_id == merchant_id,
                MallStyleGenerationQueueItem.opc_id == opc_id,
                MallStyleGenerationQueueItem.receipt_id == receipt.id,
                MallStyleGenerationQueueItem.product_id == product_id,
            )
        )
        return self._project(receipt, queue)

    def by_job(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        job_id: UUID,
    ) -> MallStyleResultListV1:
        queue = self._session.scalar(
            select(MallStyleGenerationQueueItem).where(
                MallStyleGenerationQueueItem.merchant_id == merchant_id,
                MallStyleGenerationQueueItem.opc_id == opc_id,
                MallStyleGenerationQueueItem.product_id == product_id,
                MallStyleGenerationQueueItem.generation_job_id == job_id,
            )
        )
        if queue is None:
            raise MallStyleResultNotFound
        receipt = self._receipt(merchant_id, opc_id, product_id, queue.receipt_id)
        return self._project(receipt, queue)

    def read_candidate(
        self,
        store: AssetContentStore,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        job_id: UUID,
        candidate_id: UUID,
    ) -> tuple[bytes, MallStyleResultCandidateV1]:
        result = self.by_job(
            merchant_id=merchant_id,
            opc_id=opc_id,
            product_id=product_id,
            job_id=job_id,
        )
        candidate = next(
            (row for row in result.candidates if row.asset_id == candidate_id), None
        )
        if candidate is None or candidate.availability != "available":
            raise MallStyleResultNotFound
        try:
            content = store.read_derivative(
                merchant_id,
                job_id,
                candidate.sequence_number,
                candidate.content_sha256,
            )
        except AssetContentStoreError as error:
            raise MallStyleResultNotFound from error
        return content, candidate

    def _receipt(
        self,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        receipt_id: UUID,
    ) -> MallStyleHandoffReceipt:
        receipt = self._session.scalar(
            select(MallStyleHandoffReceipt).where(
                MallStyleHandoffReceipt.id == receipt_id,
                MallStyleHandoffReceipt.merchant_id == merchant_id,
                MallStyleHandoffReceipt.opc_id == opc_id,
            )
        )
        if receipt is None or _receipt_product_id(receipt) != product_id:
            raise MallStyleResultNotFound
        return receipt

    def _project(
        self,
        receipt: MallStyleHandoffReceipt,
        queue: MallStyleGenerationQueueItem | None,
    ) -> MallStyleResultListV1:
        updated_at = _utc(receipt.updated_at)
        if queue is None:
            return _result_response(
                receipt,
                status=cast(
                    Literal[
                        "awaiting_product_photo",
                        "awaiting_subject_confirmation",
                        "awaiting_generation_approval",
                    ],
                    receipt.status,
                ),
                status_reason=receipt.status,
                updated_at=updated_at,
            )
        authority_reason = self._authority_failure(queue, receipt)
        job = self._bound_job(queue) if queue.generation_job_id is not None else None
        availability: Literal["available", "revoked", "expired"]
        if authority_reason is not None or queue.status == "revoked":
            status = "revoked"
            reason = authority_reason or queue.failure_reason or "approval_revoked"
            availability = "revoked"
        elif queue.status == "expired" or (
            _utc(queue.approval_expires_at) <= datetime.now(UTC)
        ):
            status = "expired"
            reason = "generation_approval_expired"
            availability = "expired"
        elif queue.status == "failed":
            status = "failed"
            reason = queue.failure_reason or "generation_request_failed"
            availability = "available"
        elif job is None:
            status = "pending_execution"
            reason = "approved_request_waiting_for_generation_worker"
            availability = "available"
        else:
            status, reason = _job_status(job)
            availability = "available"
        candidates = (
            self._candidate_rows(receipt, queue, job, availability)
            if job is not None
            else ()
        )
        return _result_response(
            receipt,
            queue=queue,
            job=job,
            status=status,
            status_reason=reason,
            candidates=candidates,
            updated_at=max(updated_at, _utc(queue.updated_at)),
        )

    def _authority_failure(
        self,
        queue: MallStyleGenerationQueueItem,
        receipt: MallStyleHandoffReceipt,
    ) -> str | None:
        asset = self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.id == queue.product_asset_id,
                ProductAsset.customer_id == queue.merchant_id,
                ProductAsset.product_id == queue.product_id,
                ProductAsset.identity == "original_real_photo",
                ProductAsset.confirmed.is_(True),
            )
        )
        binding = self._session.scalar(
            select(MallStyleSourceAssetBinding).where(
                MallStyleSourceAssetBinding.merchant_id == queue.merchant_id,
                MallStyleSourceAssetBinding.opc_id == queue.opc_id,
                MallStyleSourceAssetBinding.style_product_asset_id
                == queue.product_asset_id,
                MallStyleSourceAssetBinding.asset_role == "product_photo",
            )
        )
        if (
            asset is None
            or binding is None
            or binding.source_content_sha256 != asset.content_sha256
        ):
            return "product_asset_unavailable"
        permission = _latest_permission(self._session, queue.merchant_id, asset.id)
        if (
            permission is None
            or not permission.transform_permission
            or permission.id != queue.rights_permission_event_id
        ):
            return "product_transform_rights_revoked"
        seal = self._session.scalar(
            select(ProductSubjectSeal).where(
                ProductSubjectSeal.id == queue.subject_seal_id,
                ProductSubjectSeal.customer_id == queue.merchant_id,
                ProductSubjectSeal.product_asset_id == asset.id,
                ProductSubjectSeal.transform_permission_event_id == permission.id,
                ProductSubjectSeal.source_sha256 == asset.content_sha256,
                ProductSubjectSeal.publishable.is_(False),
            )
        )
        confirmation = self._session.scalar(
            select(MallStyleSubjectConfirmation).where(
                MallStyleSubjectConfirmation.receipt_id == receipt.id,
                MallStyleSubjectConfirmation.merchant_id == queue.merchant_id,
                MallStyleSubjectConfirmation.opc_id == queue.opc_id,
                MallStyleSubjectConfirmation.reviewed_subject_seal_id
                == queue.subject_seal_id,
                MallStyleSubjectConfirmation.source_sha256 == asset.content_sha256,
            )
        )
        if seal is None or confirmation is None:
            return "subject_confirmation_invalidated"
        available = MallStyleSelectionService(self._session).list_available(
            merchant_id=queue.merchant_id, opc_id=queue.opc_id
        )
        if not any(
            row.scene_style_id == queue.scene_style_id
            and row.style_card_version_id == queue.style_card_version_id
            and row.slot_key == queue.slot_key
            for row in available.selections
        ):
            return "style_selection_invalidated"
        if (
            receipt.product_asset_id != queue.product_asset_id
            or receipt.subject_seal_id != queue.subject_seal_id
        ):
            return "receipt_lineage_invalidated"
        return None

    def _bound_job(self, queue: MallStyleGenerationQueueItem) -> ImageOutputJob:
        assert queue.generation_job_id is not None
        job = self._session.scalar(
            select(ImageOutputJob).where(
                ImageOutputJob.id == queue.generation_job_id,
                ImageOutputJob.customer_id == queue.merchant_id,
                ImageOutputJob.output_type == "product_style",
                ImageOutputJob.subject_seal_id == queue.subject_seal_id,
                ImageOutputJob.scene_style_id == queue.scene_style_id,
                ImageOutputJob.style_card_version_id == queue.style_card_version_id,
            )
        )
        if job is None or not _job_metadata_matches(job, queue):
            raise MallStyleResultNotFound
        return job

    def _candidate_rows(
        self,
        receipt: MallStyleHandoffReceipt,
        queue: MallStyleGenerationQueueItem,
        job: ImageOutputJob,
        availability: Literal["available", "revoked", "expired"],
    ) -> tuple[MallStyleResultCandidateV1, ...]:
        reviews = {
            row.image_candidate_id: row
            for row in self._session.scalars(
                select(ImageReview).where(ImageReview.customer_id == queue.merchant_id)
            )
        }
        rows: list[MallStyleResultCandidateV1] = []
        for candidate in self._session.scalars(
            select(ImageCandidate)
            .where(
                ImageCandidate.customer_id == queue.merchant_id,
                ImageCandidate.output_job_id == job.id,
            )
            .order_by(ImageCandidate.sequence_number)
        ):
            if not _candidate_metadata_matches(candidate, queue):
                raise MallStyleResultNotFound
            review = reviews.get(candidate.id)
            approval_state: Literal["pending_review", "approved", "rejected"] = (
                cast(Literal["approved", "rejected"], review.decision)
                if review is not None
                else "pending_review"
            )
            rows.append(
                MallStyleResultCandidateV1(
                    asset_id=candidate.id,
                    content_sha256=candidate.content_sha256,
                    mime_type=cast(
                        Literal["image/jpeg", "image/png"], candidate.mime_type
                    ),
                    width=candidate.width,
                    height=candidate.height,
                    generation_job_id=job.id,
                    sequence_number=candidate.sequence_number,
                    adapter_version=candidate.adapter_version,
                    style_card_version_id=queue.style_card_version_id,
                    scene_style_id=queue.scene_style_id,
                    slot_key=queue.slot_key,
                    rights_reference=queue.rights_reference,
                    generation_approval_reference=queue.approval_reference,
                    approval_state=approval_state,
                    availability=availability,
                    content_url=(
                        f"/internal/mall-style/v1/products/{queue.product_id}/jobs/"
                        f"{job.id}/candidates/{candidate.id}/content"
                        if availability == "available"
                        else None
                    ),
                    publishable=False,
                    created_at=_utc(candidate.created_at),
                )
            )
        return tuple(rows)


def _receipt_product_id(receipt: MallStyleHandoffReceipt) -> str:
    product_id = receipt.payload.get("product_id")
    if not isinstance(product_id, str) or not product_id:
        raise MallStyleResultNotFound
    return product_id


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


def _job_metadata_matches(
    job: ImageOutputJob, queue: MallStyleGenerationQueueItem
) -> bool:
    metadata = job.request_metadata
    return (
        metadata.get("publishable") is False
        and metadata.get("mall_generation_request_id") == str(queue.id)
        and metadata.get("generation_approval_reference") == queue.approval_reference
        and metadata.get("rights_reference") == queue.rights_reference
        and metadata.get("provider") == queue.provider
        and metadata.get("model") == queue.model
        and metadata.get("source_product_ids") == [queue.product_id]
        and metadata.get("source_asset_ids") == [str(queue.product_asset_id)]
        and metadata.get("scene_style_id") == str(queue.scene_style_id)
        and metadata.get("style_card_version_id") == str(queue.style_card_version_id)
        and metadata.get("slot_key") == queue.slot_key
    )


def _candidate_metadata_matches(
    candidate: ImageCandidate, queue: MallStyleGenerationQueueItem
) -> bool:
    metadata = candidate.artifact_metadata
    return (
        candidate.mime_type in {"image/jpeg", "image/png"}
        and 1 <= candidate.sequence_number <= queue.candidate_count
        and metadata.get("publishable") is False
        and metadata.get("mall_generation_request_id") == str(queue.id)
        and metadata.get("generation_approval_reference") == queue.approval_reference
        and metadata.get("rights_reference") == queue.rights_reference
        and metadata.get("source_product_ids") == [queue.product_id]
        and metadata.get("source_asset_ids") == [str(queue.product_asset_id)]
        and metadata.get("scene_style_id") == str(queue.scene_style_id)
        and metadata.get("style_card_version_id") == str(queue.style_card_version_id)
        and metadata.get("slot_key") == queue.slot_key
    )


def _job_status(job: ImageOutputJob) -> tuple[str, str]:
    if job.status in {"failed", "generation_failed", "authenticity_failed"}:
        return "failed", job.failure_reason or job.status
    if job.status in {"customer_approved", "customer_rejected"}:
        return "completed", job.status
    if job.status in {"awaiting_customer_review", "machine_check_passed"}:
        return "awaiting_review", job.status
    return "generation_in_progress", job.status


def _queue_response(
    row: MallStyleGenerationQueueItem,
    *,
    disposition: Literal["accepted", "duplicate"],
) -> MallStyleGenerationQueueReceiptV1:
    if row.status != "pending_execution" or row.generation_job_id is not None:
        raise MallStyleGenerationApprovalConflict(
            "mall_style_generation_approval_already_dispatched"
        )
    return MallStyleGenerationQueueReceiptV1(
        generation_request_id=row.id,
        receipt_id=row.receipt_id,
        merchant_id=row.merchant_id,
        opc_id=row.opc_id,
        product_id=row.product_id,
        approval_reference=row.approval_reference,
        rights_reference=row.rights_reference,
        provider=row.provider,
        model=row.model,
        owner_actor_reference=row.owner_actor_reference,
        actor_role="merchant_owner",
        automatic_retry_allowed=False,
        approval_expires_at=_utc(row.approval_expires_at),
        candidate_count=row.candidate_count,
        maximum_cost_usd=row.maximum_cost_usd,
        status="pending_execution",
        generation_job_id=None,
        disposition=disposition,
        publishable=False,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _result_response(
    receipt: MallStyleHandoffReceipt,
    *,
    status: str,
    status_reason: str,
    updated_at: datetime,
    queue: MallStyleGenerationQueueItem | None = None,
    job: ImageOutputJob | None = None,
    candidates: tuple[MallStyleResultCandidateV1, ...] = (),
) -> MallStyleResultListV1:
    return MallStyleResultListV1.model_validate(
        {
            "merchant_id": receipt.merchant_id,
            "opc_id": receipt.opc_id,
            "product_id": _receipt_product_id(receipt),
            "receipt_id": receipt.id,
            "generation_request_id": queue.id if queue else None,
            "generation_job_id": job.id if job else None,
            "status": status,
            "status_reason": status_reason,
            "product_asset_id": receipt.product_asset_id,
            "subject_seal_id": receipt.subject_seal_id,
            "style_card_version_id": receipt.style_card_version_id,
            "scene_style_id": receipt.scene_style_id,
            "slot_key": receipt.slot_key,
            "rights_reference": queue.rights_reference if queue else None,
            "generation_approval_reference": (
                queue.approval_reference if queue else None
            ),
            "candidates": candidates,
            "publishable": False,
            "updated_at": updated_at,
        }
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
