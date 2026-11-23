"""Customer review rounds with durable ownership and capped regeneration."""

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.authenticity import DeterministicLocalAuthenticityChecker
from opc_style.domain.image_generator import ImageGenerator
from opc_style.models import (
    ApprovedImageAssociation,
    AuditEvent,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    ImageReviewRound,
    ImageReviewSession,
    ImageReviewSessionJob,
    ProductAsset,
)
from opc_style.repositories import ProductImageRepository, ReviewSessionRepository
from opc_style.services.authenticity import AuthenticityService
from opc_style.services.image_jobs import (
    ImageGenerationInProgressError,
    ImageJobService,
    _local_store_matches,
)
from opc_style.services.image_reviews import (
    ImageReviewConflictError,
    ImageReviewNotFoundError,
    ImageReviewOutcomeUncertainError,
    ImageReviewOutput,
    ImageReviewService,
    ProductImageViewOutput,
    ReviewDecision,
)
from opc_style.services.real_photos import AssetContentStore


@dataclass(frozen=True)
class LocalReviewJobOutput:
    """A requested job with accepted local provenance and optional review cursor."""

    job: ImageOutputJob
    handoff_id: UUID
    session: ImageReviewSession | None
    candidates: tuple[ImageCandidate, ...]
    source_assets: tuple[ProductAsset, ...]
    approved_candidate_id: UUID | None
    approved_associations: tuple[ApprovedImageAssociation, ...]


@dataclass(frozen=True)
class ReviewSessionOutput:
    session: ImageReviewSession
    current_job: ImageOutputJob
    candidates: tuple[ImageCandidate, ...]
    rounds: tuple[ImageReviewRound, ...]
    created: bool


@dataclass(frozen=True)
class ReviewDecisionOutput:
    session: ImageReviewSession
    review: ImageReview
    round: ImageReviewRound
    next_job: ImageOutputJob | None
    candidates: tuple[ImageCandidate, ...]
    created: bool
    image_review: ImageReviewOutput


class ReviewSessionService:
    """Keep immutable decisions while advancing one leased session cursor."""

    MAX_REGENERATIONS = 3

    def __init__(
        self,
        session: Session,
        content_store: AssetContentStore,
        generator: ImageGenerator,
    ) -> None:
        self._session = session
        self._content_store = content_store
        self._repository = ReviewSessionRepository(session)
        self._images = ProductImageRepository(session)
        self._jobs = ImageJobService(session, content_store, generator)
        self._reviews = ImageReviewService(session, content_store)
        self._authenticity = AuthenticityService(
            session, content_store, DeterministicLocalAuthenticityChecker()
        )

    def _accepted_handoffs(self) -> list[AuditEvent]:
        return [
            event
            for event in self._session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "classification_bridge.accepted"
                )
            )
            if event.payload.get("source") == "classification_local_page"
            and event.payload.get("desensitized_test_only") is True
            and event.payload.get("publishable") is False
        ]

    def _provenance(self, job_id: UUID) -> tuple[UUID, UUID]:
        """Only accepted handoffs or their permanently bound descendants grant access."""
        bindings = list(
            self._session.scalars(
                select(ImageReviewSessionJob).where(
                    ImageReviewSessionJob.job_id == job_id
                )
            )
        )
        if len(bindings) > 1:
            raise ImageReviewNotFoundError
        bound = (
            self._session.get(ImageReviewSession, bindings[0].session_id)
            if bindings
            else None
        )
        matches: set[tuple[UUID, UUID]] = set()
        for event in self._accepted_handoffs():
            try:
                handoff_id = UUID(str(event.payload["handoff_id"]))
                initial_jobs = {
                    UUID(value) for value in event.payload["output_job_ids"]
                }
            except (KeyError, TypeError, ValueError):
                continue
            if bound is not None:
                if (
                    bound.customer_id != event.customer_id
                    or bound.initial_handoff_id != handoff_id
                ):
                    continue
                initial_binding = self._session.scalar(
                    select(ImageReviewSessionJob).where(
                        ImageReviewSessionJob.customer_id == event.customer_id,
                        ImageReviewSessionJob.session_id == bound.id,
                        ImageReviewSessionJob.job_id.in_(initial_jobs),
                    )
                )
                if initial_binding is None:
                    continue
            elif job_id not in initial_jobs:
                continue
            matches.add((event.customer_id, handoff_id))
        if len(matches) != 1:
            raise ImageReviewNotFoundError
        return next(iter(matches))

    def get_job(
        self, job_id: UUID, *, open_review: bool = True
    ) -> LocalReviewJobOutput:
        customer_id, handoff_id = self._provenance(job_id)
        job = self._images.get_output_job(customer_id, job_id)
        if job is None:
            raise ImageReviewNotFoundError
        assets = tuple(
            asset
            for link in self._images.list_asset_links(customer_id, job_id)
            if (asset := self._images.get_asset(customer_id, link.product_asset_id))
            is not None
        )
        row = self._bound_session(customer_id, job_id)
        if (
            open_review
            and row is None
            and job.output_type in {"scene", "multi_product"}
            and job.status == "awaiting_customer_review"
        ):
            if not assets:
                raise ImageReviewNotFoundError
            row = self.open(
                customer_id, handoff_id, job_id, assets[0].product_id
            ).session
        candidates = tuple(self._images.list_candidates(customer_id, job_id))
        approved_candidate_id = None
        approved_associations: tuple[ApprovedImageAssociation, ...] = ()
        if job.status == "customer_approved":
            adopted = [
                candidate
                for candidate in candidates
                if (review := self._images.get_review(customer_id, candidate.id))
                is not None
                and review.decision == "approved"
            ]
            if len(adopted) != 1:
                raise ImageReviewOutcomeUncertainError
            approved_candidate_id = adopted[0].id
            approved_associations = tuple(
                self._images.list_approved_associations_for_candidate(
                    customer_id, adopted[0].id
                )
            )
            if not approved_associations:
                raise ImageReviewOutcomeUncertainError
        return LocalReviewJobOutput(
            job=job,
            handoff_id=handoff_id,
            session=row,
            candidates=candidates,
            source_assets=assets,
            approved_candidate_id=approved_candidate_id,
            approved_associations=approved_associations,
        )

    def read_candidate(self, job_id: UUID, candidate_id: UUID) -> tuple[bytes, str]:
        output = self.get_job(job_id, open_review=False)
        candidate = next(
            (item for item in output.candidates if item.id == candidate_id), None
        )
        if candidate is None:
            raise ImageReviewNotFoundError
        job = output.job
        if (
            not _local_store_matches(job.id, self._content_store)
            or job.artifact_store_identity != self._content_store.store_identity
            or job.artifact_store_namespace != self._content_store.store_namespace
        ):
            raise ImageReviewOutcomeUncertainError
        return self._content_store.read_derivative(
            job.customer_id, job.id, candidate.sequence_number, candidate.content_sha256
        ), candidate.mime_type

    def read_real_asset(self, job_id: UUID, asset_id: UUID) -> tuple[bytes, str]:
        output = self.get_job(job_id, open_review=False)
        asset = next(
            (
                item
                for item in output.source_assets
                if item.id == asset_id
                and item.identity == "original_real_photo"
                and item.confirmed
            ),
            None,
        )
        if asset is None:
            raise ImageReviewNotFoundError
        return self._content_store.read_original(
            output.job.customer_id, asset.id, asset.content_sha256
        ), asset.mime_type

    def get_product_images(
        self, handoff_id: UUID, product_id: str, *, view: Literal["default", "real"]
    ) -> tuple[ProductImageViewOutput, dict[UUID, UUID]]:
        events = [
            event
            for event in self._accepted_handoffs()
            if event.payload.get("handoff_id") == str(handoff_id)
        ]
        customers = {event.customer_id for event in events}
        if len(customers) != 1:
            raise ImageReviewNotFoundError
        customer_id = next(iter(customers))
        asset_jobs: dict[UUID, UUID] = {}
        for event in events:
            for raw_id in event.payload.get("output_job_ids", []):
                output = self.get_job(UUID(raw_id), open_review=False)
                if output.job.customer_id == customer_id:
                    for asset in output.source_assets:
                        if (
                            asset.product_id == product_id
                            and asset.identity == "original_real_photo"
                        ):
                            asset_jobs[asset.id] = output.job.id
        if not asset_jobs:
            raise ImageReviewNotFoundError
        result = self._reviews.get_product_images(customer_id, product_id, view=view)
        if result.style_candidate is not None:
            try:
                candidate_provenance = self._provenance(
                    result.style_candidate.output_job_id
                )
            except ImageReviewNotFoundError:
                candidate_provenance = None
            if candidate_provenance != (customer_id, handoff_id):
                result = replace(
                    result, resolved_view="real_photo", style_candidate=None
                )
        return replace(
            result,
            real_assets=tuple(
                asset for asset in result.real_assets if asset.id in asset_jobs
            ),
        ), asset_jobs

    def open(
        self,
        customer_id: UUID,
        handoff_id: UUID,
        job_id: UUID,
        product_id: str,
    ) -> ReviewSessionOutput:
        """Open or replay one product workflow, hiding foreign identities."""
        job = self._images.get_output_job(customer_id, job_id)
        if job is None:
            raise ImageReviewNotFoundError
        links = self._images.list_asset_links(customer_id, job.id)
        assets = [
            self._images.get_asset(customer_id, link.product_asset_id) for link in links
        ]
        if not any(asset and asset.product_id == product_id for asset in assets):
            raise ImageReviewNotFoundError
        bound = self._bound_session(customer_id, job_id)
        if bound is not None:
            if bound.product_id != product_id:
                raise ImageReviewConflictError("review_session_product_conflict")
            return self._session_output(bound, created=False)
        existing = self._session.scalar(
            select(ImageReviewSession).where(
                ImageReviewSession.initial_job_id == job_id,
            )
        )
        if existing is not None:
            self._validate_open_replay(existing, customer_id, job_id, product_id)
            return self._session_output(existing, created=False)
        if job.status != "awaiting_customer_review" or job.output_type not in {
            "scene",
            "multi_product",
        }:
            raise ImageReviewConflictError("review_job_not_awaiting_customer")
        row = ImageReviewSession(
            customer_id=customer_id,
            initial_handoff_id=handoff_id,
            initial_job_id=job_id,
            current_job_id=job_id,
            product_id=product_id,
        )
        self._session.add(row)
        try:
            self._session.flush()
            self._session.add(
                ImageReviewSessionJob(
                    customer_id=customer_id,
                    job_id=job_id,
                    session_id=row.id,
                )
            )
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            bound = self._bound_session(customer_id, job_id)
            if bound is not None:
                if bound.product_id != product_id:
                    raise ImageReviewConflictError("review_session_product_conflict")
                return self._session_output(bound, created=False)
            existing = self._session.scalar(
                select(ImageReviewSession).where(
                    ImageReviewSession.initial_job_id == job_id,
                )
            )
            if existing is None:
                raise
            self._validate_open_replay(existing, customer_id, job_id, product_id)
            return self._session_output(existing, created=False)
        return self._session_output(row, created=True)

    def decide(
        self,
        customer_id: UUID,
        session_id: UUID,
        candidate_id: UUID,
        *,
        decision: ReviewDecision,
        reason: str | None,
        idempotency_key: str,
    ) -> ReviewDecisionOutput:
        """Persist one decision and at most one next job, even across retries."""
        if (
            decision
            not in {"approved", "not_my_style", "product_detail_inaccurate", "rejected"}
            or not idempotency_key
            or len(idempotency_key) > 255
        ):
            raise ImageReviewConflictError("invalid_review_decision")
        self._session.expire_all()
        row = self._repository.get_for_update(customer_id, session_id)
        if row is None:
            raise ImageReviewNotFoundError
        existing = self._round_by_key(customer_id, idempotency_key)
        if existing is not None:
            review = self._session.get(ImageReview, existing.image_review_id)
            if (
                existing.session_id != session_id
                or existing.image_candidate_id != candidate_id
                or review is None
                or review.decision != decision
                or review.reason != reason
            ):
                raise ImageReviewConflictError("review_idempotency_conflict")
            if self._is_complete(row, existing, review):
                return self._decision_output(row, existing, review, created=False)
        candidate = self._images.get_candidate(customer_id, candidate_id)
        if candidate is None:
            raise ImageReviewNotFoundError
        if candidate.output_job_id != row.current_job_id:
            raise ImageReviewConflictError("review_candidate_not_current")
        if row.status not in {"awaiting_review", "regenerating"}:
            raise ImageReviewConflictError("review_session_already_closed")
        claimed = self._repository.claim_for_generation(
            customer_id, session_id, expected_revision=row.revision
        )
        if claimed is None:
            self._session.rollback()
            raise ImageGenerationInProgressError
        owner = claimed.claim_owner_token
        if owner is None:
            raise ImageReviewOutcomeUncertainError
        try:
            # Commit before the review service's commits and all generation.
            self._session.commit()
            if existing is None:
                reviewed = self._reviews.decide(
                    customer_id,
                    candidate.output_job_id,
                    candidate_id,
                    decision=decision,
                    reason=reason,
                    idempotency_key=self._review_key(idempotency_key),
                    # The final rejected round stays available for human review.
                    # Earlier rounds retain the base service's release policy.
                    retain_rejected_store=(
                        claimed.regeneration_count >= self.MAX_REGENERATIONS
                    ),
                )
                self._require_owner(customer_id, session_id, owner)
                existing = ImageReviewRound(
                    customer_id=customer_id,
                    session_id=session_id,
                    round_number=claimed.regeneration_count + 1,
                    output_job_id=candidate.output_job_id,
                    image_review_id=reviewed.review.id,
                    image_candidate_id=candidate_id,
                    idempotency_key=idempotency_key,
                )
                self._session.add(existing)
                self._session.commit()
            review = self._session.get(ImageReview, existing.image_review_id)
            if review is None:
                raise ImageReviewOutcomeUncertainError
            if decision == "approved" or existing.round_number > self.MAX_REGENERATIONS:
                finished = self._repository.release_generation_claim(
                    customer_id, session_id, owner_token=owner
                )
                if finished is None:
                    raise ImageGenerationInProgressError
                finished.status = (
                    "approved"
                    if decision == "approved"
                    else "manual_attention_required"
                )
                self._session.commit()
            else:
                self._require_owner(customer_id, session_id, owner)
                generated = self._jobs.regenerate_from_job(
                    customer_id,
                    existing.output_job_id,
                    idempotency_key=self._generation_key(existing),
                )
                self._require_owner(customer_id, session_id, owner)
                self._bind_job(customer_id, generated.job.id, session_id)
                if generated.job.status in {
                    "machine_check",
                    "awaiting_customer_review",
                }:
                    self._authenticity.run(customer_id, generated.job.id)
                finished = self._repository.complete_generation_claim(
                    customer_id,
                    session_id,
                    owner_token=owner,
                    current_job_id=generated.job.id,
                )
                if finished is None:
                    raise ImageGenerationInProgressError
                finished.regeneration_count = existing.round_number
                if generated.job.status != "awaiting_customer_review":
                    finished.status = "manual_attention_required"
                self._session.commit()
            return self._decision_output(finished, existing, review, created=True)
        except Exception:
            self._session.rollback()
            self._repository.release_generation_claim(
                customer_id, session_id, owner_token=owner
            )
            self._session.commit()
            raise

    def _require_owner(self, customer_id: UUID, session_id: UUID, owner: str) -> None:
        self._session.expire_all()
        row = self._repository.get_for_update(customer_id, session_id)
        if (
            row is None
            or row.status != "regenerating"
            or row.claim_owner_token != owner
        ):
            raise ImageGenerationInProgressError

    def _round_by_key(self, customer_id: UUID, key: str) -> ImageReviewRound | None:
        return self._session.scalar(
            select(ImageReviewRound).where(
                ImageReviewRound.customer_id == customer_id,
                ImageReviewRound.idempotency_key == key,
            )
        )

    def _bound_session(
        self, customer_id: UUID, job_id: UUID
    ) -> ImageReviewSession | None:
        binding = self._session.scalar(
            select(ImageReviewSessionJob).where(
                ImageReviewSessionJob.customer_id == customer_id,
                ImageReviewSessionJob.job_id == job_id,
            )
        )
        if binding is None:
            return None
        row = self._repository.get_for_update(customer_id, binding.session_id)
        if row is None:
            raise ImageReviewOutcomeUncertainError
        return row

    def _bind_job(self, customer_id: UUID, job_id: UUID, session_id: UUID) -> None:
        bound = self._bound_session(customer_id, job_id)
        if bound is not None:
            if bound.id != session_id:
                raise ImageReviewConflictError("review_job_already_bound")
            return
        self._session.add(
            ImageReviewSessionJob(
                customer_id=customer_id,
                job_id=job_id,
                session_id=session_id,
            )
        )
        self._session.flush()

    def _is_complete(
        self,
        row: ImageReviewSession,
        round_row: ImageReviewRound,
        review: ImageReview,
    ) -> bool:
        if review.decision == "approved":
            return row.status == "approved"
        if round_row.round_number > self.MAX_REGENERATIONS:
            return row.status == "manual_attention_required"
        return row.regeneration_count >= round_row.round_number

    def _decision_output(
        self,
        row: ImageReviewSession,
        round_row: ImageReviewRound,
        review: ImageReview,
        *,
        created: bool,
    ) -> ReviewDecisionOutput:
        next_job = None
        if (
            review.decision != "approved"
            and round_row.round_number <= self.MAX_REGENERATIONS
        ):
            next_job = self._images.get_output_job_by_idempotency_key(
                row.customer_id, self._generation_key(round_row)
            )
            if next_job is None:
                raise ImageReviewOutcomeUncertainError
        return ReviewDecisionOutput(
            session=row,
            review=review,
            round=round_row,
            next_job=next_job,
            candidates=tuple(
                self._images.list_candidates(
                    row.customer_id,
                    next_job.id if next_job else round_row.output_job_id,
                )
            ),
            created=created,
            image_review=self._reviews.decide(
                row.customer_id,
                round_row.output_job_id,
                round_row.image_candidate_id,
                decision=cast(ReviewDecision, review.decision),
                reason=review.reason,
                idempotency_key=review.idempotency_key,
            ),
        )

    def _session_output(
        self, row: ImageReviewSession, *, created: bool
    ) -> ReviewSessionOutput:
        job = self._images.get_output_job(row.customer_id, row.current_job_id)
        if job is None:
            raise ImageReviewNotFoundError
        return ReviewSessionOutput(
            session=row,
            current_job=job,
            candidates=tuple(self._images.list_candidates(row.customer_id, job.id)),
            rounds=tuple(
                self._session.scalars(
                    select(ImageReviewRound)
                    .where(
                        ImageReviewRound.customer_id == row.customer_id,
                        ImageReviewRound.session_id == row.id,
                    )
                    .order_by(ImageReviewRound.round_number)
                )
            ),
            created=created,
        )

    def _validate_open_replay(
        self,
        row: ImageReviewSession,
        customer_id: UUID,
        job_id: UUID,
        product_id: str,
    ) -> None:
        if row.customer_id != customer_id:
            raise ImageReviewNotFoundError
        if row.product_id != product_id or row.initial_job_id != job_id:
            raise ImageReviewConflictError("review_session_handoff_conflict")

    @staticmethod
    def _review_key(key: str) -> str:
        # The immutable review must fence this customer's key before any
        # session-specific round or approval association can be committed.
        return f"review-session:{sha256(key.encode()).hexdigest()}"

    @staticmethod
    def _generation_key(round_row: ImageReviewRound) -> str:
        return f"review-session:{round_row.session_id}:round:{round_row.round_number}"
