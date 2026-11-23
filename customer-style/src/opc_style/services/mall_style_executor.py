"""Durable, once-only executor. Defaults to zero paid dispatches.

A crash after reservation is ambiguous and never auto-retried, even if the
provider did not actually receive the request. This trades availability for a
hard upper bound on charges under network/process failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.identity_background_generator import (
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    BackgroundGenerator,
    BackgroundSafetyResult,
    GeneratedBackground,
)
from opc_style.domain.subject_seal import SubjectSegmenter
from opc_style.models import (
    AuditEvent,
    MallStyleExecution,
    MallStyleGenerationQueueItem,
    MallStyleHandoffReceipt,
    ProductAsset,
    ProductSubjectSeal,
    SceneStyle,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
)
from opc_style.production_settings import ProductionSettings
from opc_style.services.identity_locked_jobs import (
    IdentityLockedJobService,
    _PngDerivativeStore,
    require_identity_locked_paid_style_ready,
)
from opc_style.services.mall_style_results_v1 import MallStyleResultService, _utc


class ManualProductionBackgroundSafetyChecker:
    """Unknown backgrounds require human review; fixture metadata is irrelevant."""

    checker_version = "production-manual-background-review-v1"

    def check(self, background: GeneratedBackground) -> BackgroundSafetyResult:
        if background.synthetic_test_only:
            return BackgroundSafetyResult(
                False, ("synthetic_background_forbidden",), self.checker_version
            )
        return BackgroundSafetyResult(
            None, ("background_human_review_required",), self.checker_version
        )


class ReviewedSubjectOnlySegmenter:
    """Never fabricate a production subject; a pre-reviewed seal is required."""

    adapter_version = "reviewed-subject-only-v1"
    synthetic_test_only = False

    def seal(self, request: Any) -> Any:
        raise BackgroundGenerationError("reviewed_subject_required")


class _GuardedGenerator:
    synthetic_test_only = False

    def __init__(self, generator: BackgroundGenerator, guard: Any) -> None:
        self._generator, self._guard = generator, guard

    def generate(
        self, request: BackgroundGenerationRequest
    ) -> list[GeneratedBackground]:
        self._guard()
        backgrounds = self._generator.generate(request)
        if any(item.synthetic_test_only for item in backgrounds):
            raise BackgroundGenerationError("synthetic_background_forbidden")
        return backgrounds


class MallStyleExecutor:
    """DB lease + irreversible fence, followed by existing identity compositing."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        store: _PngDerivativeStore,
        settings: ProductionSettings,
        *,
        generator: BackgroundGenerator | None = None,
        segmenter: SubjectSegmenter | None = None,
        stop_event: Event | None = None,
    ) -> None:
        self._sessions, self._store, self._settings = sessions, store, settings
        self._generator = generator
        self._segmenter = segmenter or ReviewedSubjectOnlySegmenter()
        self._stop = stop_event or Event()
        self._owned_leases: dict[str, str] = {}

    def run_once(self) -> str:
        if self._stop.is_set():
            return "stopping"
        if not self._settings.executor_enabled or self._generator is None:
            return "provider_disabled"
        if getattr(self._generator, "synthetic_test_only", True):
            return "synthetic_provider_forbidden"
        with self._sessions() as session:
            rows = session.scalars(
                select(MallStyleGenerationQueueItem)
                .where(MallStyleGenerationQueueItem.status == "pending_execution")
                .order_by(MallStyleGenerationQueueItem.created_at)
                .limit(100)
            ).all()
            for row in rows:
                if self._stop.is_set():
                    return "stopping"
                claim = self._claim(session, row)
                if claim is None:
                    continue
                result = self._execute(session, row, claim)
                if result != "stopping":
                    self._owned_leases.pop(str(claim.id), None)
                return result
        return "idle"

    def _claim(
        self, session: Session, queue: MallStyleGenerationQueueItem
    ) -> MallStyleExecution | None:
        if self._stop.is_set():
            return None
        now = datetime.now(UTC)
        existing = session.scalar(
            select(MallStyleExecution).where(MallStyleExecution.queue_id == queue.id)
        )
        token = str(uuid4())
        if existing is None:
            existing = MallStyleExecution(
                id=uuid4(),
                queue_id=queue.id,
                claim_token=token,
                lease_expires_at=now + timedelta(seconds=self._settings.lease_seconds),
                state="leased",
                outbound_attempts=0,
                planned_job_id=uuid4(),
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None
            self._owned_leases[str(existing.id)] = token
            return existing
        if existing.outbound_attempts:
            # A durable reservation already consumed this approval. No reclaim.
            return None
        if existing.state != "blocked" and _utc(existing.lease_expires_at) > now:
            return None
        statement = (
            update(MallStyleExecution)
            .where(
                MallStyleExecution.id == existing.id,
                MallStyleExecution.claim_token == existing.claim_token,
                MallStyleExecution.outbound_attempts == 0,
                MallStyleExecution.lease_expires_at <= now,
            )
            .values(
                claim_token=token,
                state="leased",
                failure_reason=None,
                lease_expires_at=now + timedelta(seconds=self._settings.lease_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        changed = session.execute(statement).rowcount  # type: ignore[attr-defined]
        session.commit()
        if changed != 1:
            return None
        session.refresh(existing)
        self._owned_leases[str(existing.id)] = token
        return existing

    def release_unreserved_leases(self) -> None:
        """Release only this process's unused claims; paid fences never reset."""
        with self._sessions() as session:
            for identity, token in self._owned_leases.items():
                session.execute(
                    update(MallStyleExecution)
                    .where(
                        MallStyleExecution.id == UUID(identity),
                        MallStyleExecution.claim_token == token,
                        MallStyleExecution.state == "leased",
                        MallStyleExecution.outbound_attempts == 0,
                    )
                    .values(
                        state="blocked",
                        failure_reason="executor_shutdown_before_dispatch",
                        lease_expires_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            session.commit()
        self._owned_leases.clear()

    def _failure(
        self, session: Session, queue: MallStyleGenerationQueueItem
    ) -> str | None:
        now = datetime.now(UTC)
        if _utc(queue.approval_expires_at) <= now:
            return "generation_approval_expired"
        if queue.actor_role != "merchant_owner" or queue.publishable:
            return "owner_authority_invalid"
        if (
            queue.provider != self._settings.provider
            or queue.model != self._settings.model
            or queue.candidate_count != self._settings.candidate_count
        ):
            return "provider_approval_mismatch"
        if (
            queue.maximum_cost_usd < self._settings.estimated_cost_usd
            or queue.maximum_cost_usd > self._settings.maximum_cost_usd
        ):
            return "cost_approval_mismatch"
        approval = session.get(AuditEvent, queue.approval_audit_event_id)
        if (
            approval is None
            or approval.customer_id != queue.merchant_id
            or approval.event_type != "mall_style_generation.owner_approved"
            or approval.payload.get("approval_payload_sha256")
            != queue.approval_payload_sha256
            or approval.payload.get("provider") != queue.provider
            or approval.payload.get("model") != queue.model
            or approval.payload.get("candidate_count") != queue.candidate_count
            or Decimal(str(approval.payload.get("maximum_cost_usd", "-1")))
            != queue.maximum_cost_usd
            or approval.payload.get("publishable") is not False
        ):
            return "approval_audit_invalid"
        receipt = session.get(MallStyleHandoffReceipt, queue.receipt_id)
        if receipt is None or (receipt.merchant_id, receipt.opc_id) != (
            queue.merchant_id,
            queue.opc_id,
        ):
            return "receipt_tenant_mismatch"
        reason = MallStyleResultService(session)._authority_failure(queue, receipt)
        if reason:
            return reason
        asset = session.get(ProductAsset, queue.product_asset_id)
        seal = session.get(ProductSubjectSeal, queue.subject_seal_id)
        if asset is None or seal is None or seal.synthetic_test_only:
            return "production_subject_required"
        scene = session.get(SceneStyle, queue.scene_style_id)
        version = session.get(StyleCardVersion, queue.style_card_version_id)
        if scene is None or version is None:
            return "style_selection_invalidated"
        try:
            slot_rules = require_identity_locked_paid_style_ready(
                session,
                customer_id=queue.merchant_id,
                scene=scene,
                version=version,
                slot_key=queue.slot_key,
            )
            if slot_rules.get("aspect_ratio") != "4:5":
                return "production_output_ratio_unsupported"
            samples = session.scalars(
                select(StyleSample)
                .join(
                    StyleCardSampleLink,
                    (StyleSample.id == StyleCardSampleLink.style_sample_id)
                    & (StyleSample.customer_id == StyleCardSampleLink.customer_id),
                )
                .where(
                    StyleCardSampleLink.style_card_version_id
                    == queue.style_card_version_id,
                    StyleSample.customer_id == queue.merchant_id,
                    StyleSample.withdrawn_at.is_(None),
                )
            ).all()
            for sample in samples:
                if (
                    sample.content_sha256 is None
                    or sample.asset_store_namespace != self._store.store_namespace
                    or not self._store.verify_style_sample_content(
                        queue.merchant_id, sample.id, sample.content_sha256
                    )
                ):
                    return "style_evidence_unavailable"
            self._store.read_original(queue.merchant_id, asset.id, asset.content_sha256)
            self._store.read_subject_artifact(
                queue.merchant_id, seal.id, "rgba", seal.normalized_subject_sha256
            )
            self._store.read_subject_artifact(
                queue.merchant_id, seal.id, "core-mask", seal.mask_sha256
            )
        except DomainRuleError as error:
            return error.code
        except Exception:  # noqa: BLE001 - fail closed at external boundary
            return "production_media_unavailable"
        return None

    def _execute(
        self,
        session: Session,
        queue: MallStyleGenerationQueueItem,
        claim: MallStyleExecution,
    ) -> str:
        if self._stop.is_set():
            return "stopping"
        session.refresh(queue)
        failure = self._failure(session, queue)
        if failure:
            claim.state, claim.failure_reason = "blocked", failure
            queue.status = (
                "expired" if failure == "generation_approval_expired" else "revoked"
            )
            queue.failure_reason = failure
            session.add(
                AuditEvent(
                    customer_id=queue.merchant_id,
                    event_type="mall_style_generation.dispatch_blocked",
                    payload={
                        "queue_id": str(queue.id),
                        "reason": failure,
                        "publishable": False,
                    },
                )
            )
            session.commit()
            return failure
        lineage = {
            "mall_generation_request_id": str(queue.id),
            "mall_receipt_id": str(queue.receipt_id),
            "merchant_id": str(queue.merchant_id),
            "opc_id": str(queue.opc_id),
            "generation_approval_reference": queue.approval_reference,
            "approval_payload_sha256": queue.approval_payload_sha256,
            "rights_reference": queue.rights_reference,
            "provider": queue.provider,
            "model": queue.model,
            "source_product_ids": [queue.product_id],
            "source_asset_ids": [str(queue.product_asset_id)],
            "scene_style_id": str(queue.scene_style_id),
            "style_card_version_id": str(queue.style_card_version_id),
            "slot_key": queue.slot_key,
            "publishable": False,
            "maximum_cost_usd": str(queue.maximum_cost_usd),
            "automatic_retry_allowed": False,
        }
        # Commit before provider access. Even a crash before the first network
        # byte now consumes this approval and requires operator reconciliation.
        if self._stop.is_set():
            return "stopping"
        changed = session.execute(
            update(MallStyleExecution)
            .where(
                MallStyleExecution.id == claim.id,
                MallStyleExecution.claim_token == claim.claim_token,
                MallStyleExecution.state == "leased",
                MallStyleExecution.outbound_attempts == 0,
                MallStyleExecution.lease_expires_at > datetime.now(UTC),
            )
            .values(state="reserved", outbound_attempts=1, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        ).rowcount  # type: ignore[attr-defined]
        if changed != 1:
            session.rollback()
            return "lease_lost"
        queue.status = "dispatched"
        session.add(
            AuditEvent(
                customer_id=queue.merchant_id,
                event_type="mall_style_generation.dispatch_reserved",
                payload=lineage,
            )
        )
        session.commit()
        session.refresh(claim)

        def guard() -> None:
            if self._stop.is_set():
                raise BackgroundGenerationError("executor_stopping")
            # The pipeline has committed its immutable job before entering here.
            session.refresh(queue)
            failure = self._failure(session, queue)
            if failure:
                raise BackgroundGenerationError(failure)
            queue.generation_job_id = claim.planned_job_id
            queue.updated_at = datetime.now(UTC)
            session.commit()

        assert self._generator is not None
        service = IdentityLockedJobService(
            session,
            self._store,
            segmenter=self._segmenter,
            background_generator=_GuardedGenerator(self._generator, guard),
            background_safety_checker=ManualProductionBackgroundSafetyChecker(),
        )
        try:
            result = service.create(
                queue.merchant_id,
                asset_id=queue.product_asset_id,
                scene_style_id=queue.scene_style_id,
                style_card_version_id=queue.style_card_version_id,
                subject_seal_id=queue.subject_seal_id,
                slot_key=queue.slot_key,
                output_ratio="4:5",
                idempotency_key=f"mall-execution:{queue.id}",
                planned_job_id=claim.planned_job_id,
                mall_lineage=lineage,
            )
            session.refresh(claim)
            if result.job.status in {"generation_failed", "generation_uncertain"}:
                claim.state = (
                    "uncertain"
                    if result.job.status == "generation_uncertain"
                    else "blocked"
                )
                claim.failure_reason = result.job.failure_reason
                queue.status, queue.failure_reason = "failed", result.job.failure_reason
            else:
                claim.state = "completed"
            claim.updated_at = datetime.now(UTC)
            session.add(
                AuditEvent(
                    customer_id=queue.merchant_id,
                    event_type="mall_style_generation.execution_finished",
                    payload={
                        **lineage,
                        "job_id": str(result.job.id),
                        "status": result.job.status,
                        "background_review": "pending_human_review",
                    },
                )
            )
            session.commit()
            return claim.state
        except Exception:  # noqa: BLE001 - fail closed at external boundary
            session.rollback()
            session.refresh(claim)
            session.refresh(queue)
            claim.state, claim.failure_reason = (
                "uncertain",
                "dispatch_requires_reconciliation",
            )
            queue.status, queue.failure_reason = "failed", claim.failure_reason
            session.add(
                AuditEvent(
                    customer_id=queue.merchant_id,
                    event_type="mall_style_generation.execution_uncertain",
                    payload=lineage,
                )
            )
            session.commit()
            return "uncertain"

    def reconcile_expired_reservations(self) -> int:
        """Mark abandoned reservations uncertain; never put them back in queue."""
        with self._sessions() as session:
            rows = session.scalars(
                select(MallStyleExecution).where(
                    MallStyleExecution.state == "reserved",
                    MallStyleExecution.outbound_attempts == 1,
                    MallStyleExecution.lease_expires_at <= datetime.now(UTC),
                )
            ).all()
            for row in rows:
                row.state, row.failure_reason = (
                    "uncertain",
                    "dispatch_requires_reconciliation",
                )
                queue = session.get(MallStyleGenerationQueueItem, row.queue_id)
                if queue is not None:
                    queue.status, queue.failure_reason = "failed", row.failure_reason
                    session.add(
                        AuditEvent(
                            customer_id=queue.merchant_id,
                            event_type="mall_style_generation.abandoned_reservation",
                            payload={
                                "queue_id": str(queue.id),
                                "automatic_retry_allowed": False,
                                "publishable": False,
                            },
                        )
                    )
            session.commit()
            return len(rows)
