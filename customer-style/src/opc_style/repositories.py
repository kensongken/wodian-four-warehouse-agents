"""Customer-scoped persistence access for style assets."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from opc_style.domain.output_rules import validate_job_status_transition
from opc_style.domain.style_rules import enforce_scene_limit
from opc_style.models import (
    ApprovedImageAssociation,
    AuditEvent,
    AuthenticityCheck,
    AuthenticityCompletion,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    ImageReviewSession,
    ProductAsset,
    ProductAssetLink,
    ProductAssetPermissionEvent,
    ProductSubjectSeal,
    SceneStyle,
    StyleCard,
    StyleSample,
)


class StyleRepository:
    """Access customer-owned style records without cross-customer visibility."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_sample(self, customer_id: UUID, sample_id: UUID) -> StyleSample | None:
        """Return a sample only when it belongs to the requesting customer."""
        return self._session.scalar(
            select(StyleSample).where(
                StyleSample.customer_id == customer_id, StyleSample.id == sample_id
            )
        )

    def get_sample_by_photo(
        self, customer_id: UUID, photo_id: str
    ) -> StyleSample | None:
        """Return a sample by photo only within the requesting customer boundary."""
        return self._session.scalar(
            select(StyleSample).where(
                StyleSample.customer_id == customer_id, StyleSample.photo_id == photo_id
            )
        )

    def list_samples(self, customer_id: UUID) -> list[StyleSample]:
        """List only samples owned by the requesting customer."""
        return list(
            self._session.scalars(
                select(StyleSample)
                .where(StyleSample.customer_id == customer_id)
                .order_by(StyleSample.created_at, StyleSample.id)
            )
        )

    def create_sample(
        self,
        customer_id: UUID,
        photo_id: str,
        *,
        authorized_at: datetime,
        consent_provenance: str,
    ) -> StyleSample:
        """Record an explicitly authorized sample with its consent provenance."""
        if not consent_provenance.strip():
            raise ValueError("consent_provenance must not be empty")
        sample = StyleSample(
            customer_id=customer_id,
            photo_id=photo_id,
            authorized_at=authorized_at,
            consent_provenance=consent_provenance,
        )
        self._session.add(sample)
        return sample

    def get_style_card(
        self, customer_id: UUID, style_card_id: UUID
    ) -> StyleCard | None:
        """Return a style card only when it belongs to the requesting customer."""
        return self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id, StyleCard.id == style_card_id
            )
        )

    def list_style_cards(self, customer_id: UUID) -> list[StyleCard]:
        """List only style cards owned by the requesting customer."""
        return list(
            self._session.scalars(
                select(StyleCard)
                .where(StyleCard.customer_id == customer_id)
                .order_by(StyleCard.created_at, StyleCard.id)
            )
        )

    def get_scene_style(self, customer_id: UUID, scene_id: UUID) -> SceneStyle | None:
        """Return a scene style only when it belongs to the requesting customer."""
        return self._session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id, SceneStyle.id == scene_id
            )
        )

    def list_scene_styles(self, customer_id: UUID) -> list[SceneStyle]:
        """List only scene styles owned by the requesting customer."""
        return list(
            self._session.scalars(
                select(SceneStyle)
                .where(SceneStyle.customer_id == customer_id)
                .order_by(SceneStyle.created_at, SceneStyle.id)
            )
        )

    def create_scene_style(
        self,
        customer_id: UUID,
        *,
        name: str,
        style_card_id: UUID | None = None,
        active: bool = True,
        rules: dict[str, Any] | None = None,
    ) -> SceneStyle:
        """Create a customer scene through the only active-scene limit gateway."""
        if active:
            active_scene_count = self._session.scalar(
                select(func.count())
                .select_from(SceneStyle)
                .where(
                    SceneStyle.customer_id == customer_id,
                    SceneStyle.active.is_(True),
                )
            )
            enforce_scene_limit(active_scene_count or 0)
        scene = SceneStyle(
            customer_id=customer_id,
            name=name,
            style_card_id=style_card_id,
            active=active,
            rules=rules or {},
        )
        self._session.add(scene)
        return scene

    def list_audit_events(self, customer_id: UUID) -> list[AuditEvent]:
        """List only audit events owned by the requesting customer."""
        statement: Select[tuple[AuditEvent]] = (
            select(AuditEvent)
            .where(AuditEvent.customer_id == customer_id)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
        return list(self._session.scalars(statement))


class ProductImageRepository:
    """Access product assets and output history within one customer boundary."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_asset(self, customer_id: UUID, asset_id: UUID) -> ProductAsset | None:
        """Return a product asset only to its owning customer."""
        return self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == customer_id, ProductAsset.id == asset_id
            )
        )

    def get_asset_by_photo_and_hash(
        self, customer_id: UUID, photo_id: str, content_sha256: str
    ) -> ProductAsset | None:
        """Find an idempotent intake replay only inside the owning customer."""
        return self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == customer_id,
                ProductAsset.photo_id == photo_id,
                ProductAsset.content_sha256 == content_sha256,
            )
        )

    def get_asset_by_photo(
        self, customer_id: UUID, photo_id: str
    ) -> ProductAsset | None:
        """Resolve one immutable photo identity inside its customer boundary."""
        return self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == customer_id,
                ProductAsset.photo_id == photo_id,
            )
        )

    def list_assets(
        self, customer_id: UUID, *, product_id: str | None = None
    ) -> list[ProductAsset]:
        """List customer-owned assets, optionally narrowed to one product."""
        statement = select(ProductAsset).where(ProductAsset.customer_id == customer_id)
        if product_id is not None:
            statement = statement.where(ProductAsset.product_id == product_id)
        return list(
            self._session.scalars(
                statement.order_by(ProductAsset.created_at, ProductAsset.id)
            )
        )

    def get_latest_asset_permission(
        self, customer_id: UUID, asset_id: UUID
    ) -> ProductAssetPermissionEvent | None:
        """Return the latest immutable permission event within the asset owner scope."""
        return self._session.scalar(
            select(ProductAssetPermissionEvent)
            .where(
                ProductAssetPermissionEvent.customer_id == customer_id,
                ProductAssetPermissionEvent.product_asset_id == asset_id,
            )
            .order_by(
                ProductAssetPermissionEvent.occurred_at.desc(),
                ProductAssetPermissionEvent.created_at.desc(),
                ProductAssetPermissionEvent.id.desc(),
            )
            .limit(1)
        )

    def get_subject_seal(
        self, customer_id: UUID, seal_id: UUID
    ) -> ProductSubjectSeal | None:
        """Return one sealed subject only inside its customer boundary."""
        return self._session.scalar(
            select(ProductSubjectSeal).where(
                ProductSubjectSeal.customer_id == customer_id,
                ProductSubjectSeal.id == seal_id,
            )
        )

    def get_subject_seal_by_fingerprint(
        self, customer_id: UUID, fingerprint: str
    ) -> ProductSubjectSeal | None:
        """Resolve an idempotent seal replay only for its owning customer."""
        return self._session.scalar(
            select(ProductSubjectSeal).where(
                ProductSubjectSeal.customer_id == customer_id,
                ProductSubjectSeal.idempotency_fingerprint == fingerprint,
            )
        )

    def get_asset_intake_permission(
        self,
        customer_id: UUID,
        asset_id: UUID,
        occurred_at: datetime,
        provenance: str = "product_asset_intake",
    ) -> ProductAssetPermissionEvent | None:
        """Return the immutable permission fact recorded by the original intake."""
        return self._session.scalar(
            select(ProductAssetPermissionEvent).where(
                ProductAssetPermissionEvent.customer_id == customer_id,
                ProductAssetPermissionEvent.product_asset_id == asset_id,
                ProductAssetPermissionEvent.occurred_at == occurred_at,
                ProductAssetPermissionEvent.provenance == provenance,
            )
        )

    def get_output_job(self, customer_id: UUID, job_id: UUID) -> ImageOutputJob | None:
        """Return an output job only to its owning customer."""
        return self._session.scalar(
            select(ImageOutputJob).where(
                ImageOutputJob.customer_id == customer_id, ImageOutputJob.id == job_id
            )
        )

    def get_output_job_by_idempotency_key(
        self, customer_id: UUID, idempotency_key: str
    ) -> ImageOutputJob | None:
        """Find a safe-retry output job within its customer boundary."""
        return self._session.scalar(
            select(ImageOutputJob).where(
                ImageOutputJob.customer_id == customer_id,
                ImageOutputJob.idempotency_key == idempotency_key,
            )
        )

    def transition_output_job(
        self,
        customer_id: UUID,
        job_id: UUID,
        new_status: str,
        *,
        failure_reason: str | None,
    ) -> ImageOutputJob | None:
        """Advance a customer-owned job through one explicitly legal transition."""
        job = self.get_output_job(customer_id, job_id)
        if job is None:
            return None
        validate_job_status_transition(job.status, new_status)
        job.status = new_status
        job.failure_reason = failure_reason
        job.updated_at = datetime.now(UTC)
        return job

    def list_output_jobs(self, customer_id: UUID) -> list[ImageOutputJob]:
        """List only output jobs owned by the requesting customer."""
        return list(
            self._session.scalars(
                select(ImageOutputJob)
                .where(ImageOutputJob.customer_id == customer_id)
                .order_by(ImageOutputJob.created_at, ImageOutputJob.id)
            )
        )

    def get_candidate(
        self, customer_id: UUID, candidate_id: UUID
    ) -> ImageCandidate | None:
        """Return a candidate only to its owning customer."""
        return self._session.scalar(
            select(ImageCandidate).where(
                ImageCandidate.customer_id == customer_id,
                ImageCandidate.id == candidate_id,
            )
        )

    def list_candidates(self, customer_id: UUID, job_id: UUID) -> list[ImageCandidate]:
        """List a customer's candidates for one customer-owned job."""
        return list(
            self._session.scalars(
                select(ImageCandidate)
                .where(
                    ImageCandidate.customer_id == customer_id,
                    ImageCandidate.output_job_id == job_id,
                )
                .order_by(ImageCandidate.sequence_number, ImageCandidate.id)
            )
        )

    def list_asset_links(
        self, customer_id: UUID, job_id: UUID
    ) -> list[ProductAssetLink]:
        """List immutable source links in their declared job position."""
        return list(
            self._session.scalars(
                select(ProductAssetLink)
                .where(
                    ProductAssetLink.customer_id == customer_id,
                    ProductAssetLink.output_job_id == job_id,
                )
                .order_by(ProductAssetLink.position, ProductAssetLink.id)
            )
        )

    def list_authenticity_checks(
        self, customer_id: UUID, candidate_id: UUID
    ) -> list[AuthenticityCheck]:
        """List immutable machine checks within the candidate's customer boundary."""
        return list(
            self._session.scalars(
                select(AuthenticityCheck)
                .where(
                    AuthenticityCheck.customer_id == customer_id,
                    AuthenticityCheck.image_candidate_id == candidate_id,
                )
                .order_by(
                    AuthenticityCheck.created_at,
                    AuthenticityCheck.check_type,
                    AuthenticityCheck.attempt,
                )
            )
        )

    def get_authenticity_completion(
        self, customer_id: UUID, job_id: UUID
    ) -> AuthenticityCompletion | None:
        """Return the append-only Task 5 batch seal for one owned job."""
        return self._session.scalar(
            select(AuthenticityCompletion).where(
                AuthenticityCompletion.customer_id == customer_id,
                AuthenticityCompletion.output_job_id == job_id,
            )
        )

    def list_authenticity_completion_events(
        self, customer_id: UUID
    ) -> list[AuditEvent]:
        """Return immutable authenticity-completion audit events for validation."""
        return list(
            self._session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.customer_id == customer_id,
                    AuditEvent.event_type == "image_output.authenticity_completed",
                )
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )

    def get_review(self, customer_id: UUID, candidate_id: UUID) -> ImageReview | None:
        """Return a candidate review only to its owning customer."""
        return self._session.scalar(
            select(ImageReview).where(
                ImageReview.customer_id == customer_id,
                ImageReview.image_candidate_id == candidate_id,
            )
        )

    def get_review_by_idempotency_key(
        self, customer_id: UUID, idempotency_key: str
    ) -> ImageReview | None:
        """Find a safely replayable review inside one customer boundary."""
        return self._session.scalar(
            select(ImageReview).where(
                ImageReview.customer_id == customer_id,
                ImageReview.idempotency_key == idempotency_key,
            )
        )

    def list_approved_associations_for_candidate(
        self, customer_id: UUID, candidate_id: UUID
    ) -> list[ApprovedImageAssociation]:
        """List immutable approval links in stable source-product order."""
        return list(
            self._session.scalars(
                select(ApprovedImageAssociation)
                .where(
                    ApprovedImageAssociation.customer_id == customer_id,
                    ApprovedImageAssociation.image_candidate_id == candidate_id,
                )
                .order_by(
                    ApprovedImageAssociation.product_id,
                    ApprovedImageAssociation.source_asset_id,
                )
            )
        )

    def list_approved_associations_for_product(
        self, customer_id: UUID, product_id: str
    ) -> list[ApprovedImageAssociation]:
        """List approved derivative links for one customer-owned product."""
        return list(
            self._session.scalars(
                select(ApprovedImageAssociation)
                .where(
                    ApprovedImageAssociation.customer_id == customer_id,
                    ApprovedImageAssociation.product_id == product_id,
                )
                .order_by(
                    ApprovedImageAssociation.created_at.desc(),
                    ApprovedImageAssociation.id.desc(),
                )
            )
        )


class ReviewSessionRepository:
    """Lock one customer-owned review workflow before it changes state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_update(
        self, customer_id: UUID, session_id: UUID
    ) -> ImageReviewSession | None:
        """Return a review session only to its owner and lock it for mutation."""
        return self._session.scalar(
            select(ImageReviewSession)
            .where(
                ImageReviewSession.customer_id == customer_id,
                ImageReviewSession.id == session_id,
            )
            .with_for_update()
        )

    def claim_for_generation(
        self,
        customer_id: UUID,
        session_id: UUID,
        *,
        expected_revision: int,
        now: datetime | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> ImageReviewSession | None:
        """Atomically grant a bounded generation lease to one worker."""
        if lease_duration <= timedelta():
            raise ValueError("lease_duration must be positive")
        claimed_at = now or datetime.now(UTC)
        owner_token = str(uuid4())
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(ImageReviewSession)
                .execution_options(synchronize_session=False)
                .where(
                    ImageReviewSession.customer_id == customer_id,
                    ImageReviewSession.id == session_id,
                    ImageReviewSession.revision == expected_revision,
                    or_(
                        ImageReviewSession.status == "awaiting_review",
                        and_(
                            ImageReviewSession.status == "regenerating",
                            ImageReviewSession.claim_expires_at <= claimed_at,
                        ),
                    ),
                )
                .values(
                    status="regenerating",
                    revision=ImageReviewSession.revision + 1,
                    claim_owner_token=owner_token,
                    claimed_at=claimed_at,
                    claim_expires_at=claimed_at + lease_duration,
                    updated_at=claimed_at,
                )
            ),
        )
        if result.rowcount != 1:
            return None
        self._session.expire_all()
        return self.get_for_update(customer_id, session_id)

    def release_generation_claim(
        self, customer_id: UUID, session_id: UUID, *, owner_token: str
    ) -> ImageReviewSession | None:
        """Release one worker's lease without allowing another owner to do so."""
        return self._finish_generation_claim(
            customer_id, session_id, owner_token=owner_token, current_job_id=None
        )

    def complete_generation_claim(
        self,
        customer_id: UUID,
        session_id: UUID,
        *,
        owner_token: str,
        current_job_id: UUID,
    ) -> ImageReviewSession | None:
        """Complete one worker's lease and persist its customer-owned job cursor."""
        return self._finish_generation_claim(
            customer_id,
            session_id,
            owner_token=owner_token,
            current_job_id=current_job_id,
        )

    def _finish_generation_claim(
        self,
        customer_id: UUID,
        session_id: UUID,
        *,
        owner_token: str,
        current_job_id: UUID | None,
    ) -> ImageReviewSession | None:
        values: dict[str, Any] = {
            "status": "awaiting_review",
            "revision": ImageReviewSession.revision + 1,
            "claim_owner_token": None,
            "claimed_at": None,
            "claim_expires_at": None,
            "updated_at": datetime.now(UTC),
        }
        if current_job_id is not None:
            values["current_job_id"] = current_job_id
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(ImageReviewSession)
                .execution_options(synchronize_session=False)
                .where(
                    ImageReviewSession.customer_id == customer_id,
                    ImageReviewSession.id == session_id,
                    ImageReviewSession.status == "regenerating",
                    ImageReviewSession.claim_owner_token == owner_token,
                )
                .values(values)
            ),
        )
        if result.rowcount != 1:
            return None
        self._session.expire_all()
        return self.get_for_update(customer_id, session_id)
