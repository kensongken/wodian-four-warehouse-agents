"""Customer review gate and immutable dual-image product associations."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.authenticity import (
    DeterministicLocalAuthenticityChecker,
    FidelityState,
    validate_fidelity_decision,
)
from opc_style.models import (
    ApprovedImageAssociation,
    AuditEvent,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    PixelCompositionReceipt,
    ProductAsset,
    ProductAssetLink,
    StyleSuiteCandidateFidelity,
    StyleSuiteCompletion,
    StyleSuiteRun,
    StyleSuiteRunItem,
)
from opc_style.providers.openai_images import REAL_MODEL_GENERATION_MODE
from opc_style.repositories import ProductImageRepository
from opc_style.services.authenticity import (
    AuthenticityOutcomeUncertainError,
    AuthenticityService,
)
from opc_style.services.image_jobs import (
    _forget_local_store,
    _local_job_lock,
    _local_store_matches,
)
from opc_style.services.real_photos import AssetContentStore

ReviewDecision = Literal[
    "approved", "not_my_style", "product_detail_inaccurate", "rejected"
]
ImageKind = Literal["real_photo", "style_image"]


class ImageReviewNotFoundError(Exception):
    """Conceal missing, foreign, or job-mismatched review identities."""


class ImageReviewConflictError(Exception):
    """Reject a second decision or an invalid review gate state."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ImageReviewOutcomeUncertainError(Exception):
    """Fail closed when artifact or transaction outcome cannot be proven."""


@dataclass(frozen=True)
class ReviewAssociationOutput:
    """One approved image association with its retained original source."""

    association: ApprovedImageAssociation
    source_asset: ProductAsset


@dataclass(frozen=True)
class ImageReviewOutput:
    """One durable decision and any associations it authorized."""

    job: ImageOutputJob
    candidate: ImageCandidate
    review: ImageReview
    image_kind: ImageKind
    source_assets: tuple[ProductAsset, ...]
    associations: tuple[ReviewAssociationOutput, ...]
    created: bool
    suite_state: str | None = None


@dataclass(frozen=True)
class ProductImageViewOutput:
    """Resolved product-image query without exposing any storage path."""

    product_id: str
    requested_view: Literal["default", "real"]
    resolved_view: Literal["style_image", "real_photo"]
    real_assets: tuple[ProductAsset, ...]
    style_candidate: ImageCandidate | None


class ImageReviewService:
    """Append exactly one customer decision after the complete machine gate."""

    def __init__(self, session: Session, content_store: AssetContentStore) -> None:
        self._session = session
        self._repository = ProductImageRepository(session)
        self._content_store = content_store

    def decide(
        self,
        customer_id: UUID,
        job_id: UUID,
        candidate_id: UUID,
        *,
        decision: ReviewDecision,
        reason: str | None,
        idempotency_key: str,
        retain_rejected_store: bool = False,
        fidelity_state: FidelityState | None = None,
        fidelity_flags: tuple[str, ...] = (),
        fidelity_warning_acknowledged: bool = False,
    ) -> ImageReviewOutput:
        """Persist one auditable decision or replay the same durable request."""
        with _local_job_lock(job_id):
            job, candidate = self._load_owned_job_candidate(
                customer_id, job_id, candidate_id
            )
            suite_item = self._session.scalar(
                select(StyleSuiteRunItem).where(
                    StyleSuiteRunItem.customer_id == customer_id,
                    StyleSuiteRunItem.output_job_id == job.id,
                )
            )
            persisted_fidelity = (
                self._session.scalar(
                    select(StyleSuiteCandidateFidelity).where(
                        StyleSuiteCandidateFidelity.customer_id == customer_id,
                        StyleSuiteCandidateFidelity.image_candidate_id == candidate.id,
                    )
                )
                if suite_item is not None
                else None
            )
            server_owned_fidelity = (
                suite_item is not None
                and job.request_metadata.get("generation_mode")
                == REAL_MODEL_GENERATION_MODE
            )
            replay = self._resolve_existing_request(
                customer_id,
                job,
                candidate,
                decision=decision,
                reason=reason,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                if suite_item is not None:
                    if server_owned_fidelity:
                        if persisted_fidelity is None:
                            raise ImageReviewConflictError(
                                "candidate_fidelity_required"
                            )
                        if fidelity_state is not None and (
                            persisted_fidelity.fidelity_state != fidelity_state
                            or tuple(persisted_fidelity.fidelity_flags)
                            != tuple(sorted(set(fidelity_flags)))
                        ):
                            raise ImageReviewConflictError(
                                "candidate_fidelity_client_override_forbidden"
                            )
                    elif (
                        fidelity_state is None
                        or persisted_fidelity is None
                        or persisted_fidelity.fidelity_state != fidelity_state
                        or tuple(persisted_fidelity.fidelity_flags)
                        != tuple(sorted(set(fidelity_flags)))
                        or persisted_fidelity.warning_acknowledged
                        != fidelity_warning_acknowledged
                    ):
                        raise ImageReviewConflictError("review_idempotency_conflict")
                return replay
            normalized_flags: tuple[str, ...] = ()
            if suite_item is not None:
                if suite_item.state == "uncertain":
                    raise ImageReviewConflictError("suite_item_needs_reconciliation")
                if server_owned_fidelity:
                    if persisted_fidelity is None:
                        raise ImageReviewConflictError("candidate_fidelity_required")
                    resolved_state = cast(
                        FidelityState, persisted_fidelity.fidelity_state
                    )
                    normalized_flags = tuple(persisted_fidelity.fidelity_flags)
                    if decision == "approved" and resolved_state == "red":
                        raise ImageReviewConflictError("candidate_fidelity_rejected")
                    if decision == "approved" and resolved_state == "unknown":
                        raise ImageReviewConflictError("candidate_fidelity_unavailable")
                    if fidelity_state is not None and (
                        fidelity_state != resolved_state
                        or tuple(sorted(set(fidelity_flags))) != normalized_flags
                    ):
                        raise ImageReviewConflictError(
                            "candidate_fidelity_client_override_forbidden"
                        )
                else:
                    if fidelity_state is None:
                        raise ImageReviewConflictError(
                            "suite_candidate_fidelity_required"
                        )
                    resolved_state = fidelity_state
                    try:
                        normalized_flags = validate_fidelity_decision(
                            resolved_state, fidelity_flags
                        )
                    except ValueError as error:
                        raise ImageReviewConflictError(str(error)) from error
                if (
                    decision == "approved"
                    and
                    resolved_state == "yellow"
                    and not fidelity_warning_acknowledged
                ):
                    raise ImageReviewConflictError(
                        "suite_candidate_fidelity_warning_acknowledgement_required"
                    )
                if decision == "approved" and resolved_state not in {"green", "yellow"}:
                    raise ImageReviewConflictError("suite_candidate_fidelity_not_green")
                if job.status == "machine_check":
                    transitioned_for_review = self._repository.transition_output_job(
                        customer_id,
                        job.id,
                        "awaiting_customer_review",
                        failure_reason=None,
                    )
                    if transitioned_for_review is None:
                        raise ImageReviewNotFoundError
                    self._session.flush()
                    job = transitioned_for_review
            pixel_preserving = (
                suite_item is not None
                and job.request_metadata.get("generation_mode")
                == "pixel_preserving_isolated"
            )
            if job.status != "awaiting_customer_review":
                if pixel_preserving and job.status == "awaiting_review":
                    # Compatibility for local pixel jobs created before they
                    # joined the shared customer-review state machine.
                    job.status = "awaiting_customer_review"
                    job.updated_at = datetime.now(UTC)
                    self._session.flush()
                else:
                    raise ImageReviewConflictError("review_job_not_awaiting_customer")

            candidates = tuple(self._repository.list_candidates(customer_id, job.id))
            candidate_ids = {item.id for item in candidates}
            batch_is_reviewable = (
                bool(candidates) if pixel_preserving else len(candidates) == 3
            )
            if not batch_is_reviewable or candidate.id not in candidate_ids:
                raise ImageReviewConflictError(
                    "candidate_machine_check_not_current_pass"
                )
            if suite_item is None:
                authenticity = AuthenticityService(
                    self._session,
                    self._content_store,
                    DeterministicLocalAuthenticityChecker(),
                )
                if not authenticity.has_trusted_pass_completion(
                    customer_id, job, candidates
                ):
                    raise ImageReviewConflictError(
                        "candidate_machine_check_not_current_pass"
                    )
                try:
                    authenticity.verify_candidate_for_customer_review(
                        customer_id, job, candidate
                    )
                except AuthenticityOutcomeUncertainError as error:
                    raise ImageReviewOutcomeUncertainError from error
            elif pixel_preserving:
                if not self._has_exact_pixel_composition_proof(candidate):
                    raise ImageReviewOutcomeUncertainError
            elif not self._has_exact_artifact_proof(job, candidate):
                raise ImageReviewOutcomeUncertainError

            source_links, source_assets = self._load_sources(customer_id, job)
            image_kind = _image_kind(job)
            review = ImageReview(
                customer_id=customer_id,
                image_candidate_id=candidate.id,
                decision=decision,
                reason=reason,
                idempotency_key=idempotency_key,
                created_at=datetime.now(UTC),
            )
            self._session.add(review)
            try:
                self._session.flush()
                association_rows: tuple[ApprovedImageAssociation, ...] = ()
                if decision == "approved":
                    association_rows = tuple(
                        ApprovedImageAssociation(
                            customer_id=customer_id,
                            image_review_id=review.id,
                            image_candidate_id=candidate.id,
                            output_job_id=job.id,
                            product_asset_link_id=link.id,
                            source_asset_id=asset.id,
                            product_id=asset.product_id,
                            image_kind=image_kind,
                            created_at=review.created_at,
                        )
                        for link, asset in zip(source_links, source_assets, strict=True)
                    )
                    self._session.add_all(association_rows)
                if suite_item is not None and decision != "approved":
                    transitioned = job
                else:
                    new_status = (
                        "customer_approved"
                        if decision == "approved"
                        else "customer_rejected"
                    )
                    maybe_transitioned = self._repository.transition_output_job(
                        customer_id, job.id, new_status, failure_reason=None
                    )
                    if maybe_transitioned is None:
                        raise ImageReviewNotFoundError
                    transitioned = maybe_transitioned
                suite_state = None
                if suite_item is not None:
                    suite_state = self._record_suite_decision(
                        customer_id,
                        suite_item,
                        candidate,
                        review,
                        resolved_state,
                        normalized_flags,
                        fidelity_warning_acknowledged,
                        persisted_fidelity=persisted_fidelity,
                    )
                self._session.add(
                    AuditEvent(
                        customer_id=customer_id,
                        event_type="image_output.customer_reviewed",
                        payload={
                            "job_id": str(job.id),
                            "candidate_id": str(candidate.id),
                            "review_id": str(review.id),
                            "decision": decision,
                            "image_kind": image_kind,
                            "source_product_ids": [
                                asset.product_id for asset in source_assets
                            ],
                            "source_asset_ids": [
                                str(asset.id) for asset in source_assets
                            ],
                            "customer_approved": decision == "approved",
                            "publishable": False,
                        },
                        occurred_at=review.created_at,
                    )
                )
                self._session.commit()
            except IntegrityError as error:
                self._session.rollback()
                recovered = self._recover_durable_request(
                    customer_id,
                    job_id,
                    candidate_id,
                    decision=decision,
                    reason=reason,
                    idempotency_key=idempotency_key,
                )
                if recovered is not None:
                    if (
                        suite_item is None
                        and recovered.review.decision != "approved"
                        and not retain_rejected_store
                    ):
                        _forget_local_store(job.id, self._content_store)
                    return recovered
                self._raise_persisted_conflict(
                    customer_id, candidate_id, idempotency_key
                )
                raise ImageReviewOutcomeUncertainError from error
            except Exception as error:
                self._session.rollback()
                recovered = self._recover_durable_request(
                    customer_id,
                    job_id,
                    candidate_id,
                    decision=decision,
                    reason=reason,
                    idempotency_key=idempotency_key,
                )
                if recovered is not None:
                    if (
                        suite_item is None
                        and recovered.review.decision != "approved"
                        and not retain_rejected_store
                    ):
                        _forget_local_store(job.id, self._content_store)
                    return recovered
                raise ImageReviewOutcomeUncertainError from error

            if suite_item is not None and decision == "approved":
                suite_state = self._finalize_suite_if_ready(
                    customer_id, suite_item.run_id, review.created_at
                )
            output = ImageReviewOutput(
                job=transitioned,
                candidate=candidate,
                review=review,
                image_kind=image_kind,
                source_assets=source_assets,
                associations=(
                    tuple(
                        ReviewAssociationOutput(association=row, source_asset=asset)
                        for row, asset in zip(
                            association_rows, source_assets, strict=True
                        )
                    )
                    if decision == "approved"
                    else ()
                ),
                created=True,
                suite_state=suite_state,
            )
            if (
                suite_item is None
                and decision != "approved"
                and not retain_rejected_store
            ):
                _forget_local_store(job.id, self._content_store)
            return output

    def _record_suite_decision(
        self,
        customer_id: UUID,
        item: StyleSuiteRunItem,
        candidate: ImageCandidate,
        review: ImageReview,
        fidelity_state: FidelityState,
        fidelity_flags: tuple[str, ...],
        warning_acknowledged: bool,
        *,
        persisted_fidelity: StyleSuiteCandidateFidelity | None,
    ) -> str:
        """Seal fidelity; select green or explicitly acknowledged yellow."""
        if candidate.output_job_id != item.output_job_id:
            raise ImageReviewConflictError("suite_candidate_lineage_mismatch")
        if persisted_fidelity is None:
            persisted_fidelity = StyleSuiteCandidateFidelity(
                customer_id=customer_id,
                run_item_id=item.id,
                output_job_id=candidate.output_job_id,
                image_candidate_id=candidate.id,
                fidelity_state=fidelity_state,
                fidelity_flags=list(fidelity_flags),
                warning_acknowledged=warning_acknowledged,
                created_at=review.created_at,
            )
            self._session.add(persisted_fidelity)
        elif (
            fidelity_state == "yellow"
            and warning_acknowledged
            and not persisted_fidelity.warning_acknowledged
        ):
            persisted_fidelity.warning_acknowledged = True
        self._session.flush()
        if review.decision == "approved":
            item.fidelity_state = fidelity_state
            item.fidelity_flags = list(fidelity_flags)
            item.selected_candidate_id = candidate.id
            item.selected_review_id = review.id
            item.state = "approved"
        run = self._session.scalar(
            select(StyleSuiteRun).where(
                StyleSuiteRun.customer_id == customer_id,
                StyleSuiteRun.id == item.run_id,
            )
        )
        if run is None:
            raise ImageReviewOutcomeUncertainError
        run.state = "awaiting_review"
        return run.state

    def _finalize_suite_if_ready(
        self, customer_id: UUID, run_id: UUID, created_at: datetime
    ) -> str:
        """Atomically issue the run's single completion after all approvals persist."""
        with _local_job_lock(run_id):
            try:
                run = self._session.scalar(
                    select(StyleSuiteRun).where(
                        StyleSuiteRun.customer_id == customer_id,
                        StyleSuiteRun.id == run_id,
                    )
                )
                if run is None:
                    raise ImageReviewOutcomeUncertainError
                existing = self._session.scalar(
                    select(StyleSuiteCompletion).where(
                        StyleSuiteCompletion.customer_id == customer_id,
                        StyleSuiteCompletion.run_id == run_id,
                    )
                )
                if existing is not None:
                    return "completed"
                items = list(
                    self._session.scalars(
                        select(StyleSuiteRunItem).where(
                            StyleSuiteRunItem.customer_id == customer_id,
                            StyleSuiteRunItem.run_id == run_id,
                        )
                    )
                )
                if not items or not all(
                    row.state == "approved"
                    and row.fidelity_state in {"green", "yellow"}
                    and row.selected_candidate_id is not None
                    and row.selected_review_id is not None
                    for row in items
                ):
                    return "awaiting_review"
                run.state = "completed"
                self._session.flush()
                self._session.add(
                    StyleSuiteCompletion(
                        customer_id=customer_id,
                        run_id=run.id,
                        version=1,
                        snapshot={
                            "run_id": str(run.id),
                            "product_id": run.product_id,
                            "source_asset_id": str(run.source_asset_id),
                            "source_content_sha256": run.source_content_sha256,
                            "items": [
                                {
                                    "item_id": str(row.id),
                                    "style_version_id": str(
                                        row.scene_style_version_id
                                    ),
                                    "slot_key": row.slot_key,
                                    "selected_candidate_id": str(
                                        row.selected_candidate_id
                                    ),
                                    "selected_review_id": str(row.selected_review_id),
                                    "fidelity_state": row.fidelity_state,
                                    "fidelity_flags": row.fidelity_flags,
                                }
                                for row in sorted(
                                    items,
                                    key=lambda value: (
                                        value.style_name,
                                        value.slot_key,
                                    ),
                                )
                            ],
                        },
                        created_at=created_at,
                    )
                )
                self._session.commit()
                return "completed"
            except IntegrityError as error:
                self._session.rollback()
                completion = self._session.scalar(
                    select(StyleSuiteCompletion).where(
                        StyleSuiteCompletion.customer_id == customer_id,
                        StyleSuiteCompletion.run_id == run_id,
                        StyleSuiteCompletion.version == 1,
                    )
                )
                run = self._session.get(StyleSuiteRun, run_id)
                if completion is not None and run is not None and run.state == "completed":
                    return "completed"
                raise ImageReviewOutcomeUncertainError from error

    def get_product_images(
        self,
        customer_id: UUID,
        product_id: str,
        *,
        view: Literal["default", "real"],
    ) -> ProductImageViewOutput:
        """Resolve style-by-default while an explicit real view returns originals."""
        real_assets = tuple(
            asset
            for asset in self._repository.list_assets(
                customer_id, product_id=product_id
            )
            if asset.identity == "original_real_photo" and asset.confirmed is True
        )
        if not real_assets:
            raise ImageReviewNotFoundError
        if view == "real":
            return ProductImageViewOutput(
                product_id=product_id,
                requested_view="real",
                resolved_view="real_photo",
                real_assets=real_assets,
                style_candidate=None,
            )
        for association in self._repository.list_approved_associations_for_product(
            customer_id, product_id
        ):
            if association.image_kind != "style_image":
                continue
            review = self._session.get(ImageReview, association.image_review_id)
            candidate = self._repository.get_candidate(
                customer_id, association.image_candidate_id
            )
            if review is None or review.decision != "approved" or candidate is None:
                continue
            job = self._repository.get_output_job(customer_id, candidate.output_job_id)
            if job is None or job.status != "customer_approved":
                continue
            if not self._has_exact_artifact_proof(job, candidate):
                raise ImageReviewOutcomeUncertainError
            return ProductImageViewOutput(
                product_id=product_id,
                requested_view="default",
                resolved_view="style_image",
                real_assets=real_assets,
                style_candidate=candidate,
            )
        return ProductImageViewOutput(
            product_id=product_id,
            requested_view="default",
            resolved_view="real_photo",
            real_assets=real_assets,
            style_candidate=None,
        )

    def _has_exact_artifact_proof(
        self, job: ImageOutputJob, candidate: ImageCandidate
    ) -> bool:
        try:
            return (
                _local_store_matches(job.id, self._content_store)
                and job.artifact_store_identity == self._content_store.store_identity
                and job.artifact_store_namespace == self._content_store.store_namespace
                and tuple(job.expected_artifact_sequences or ()) == (1, 2, 3)
                and self._content_store.verify_derivative(
                    job.customer_id,
                    job.id,
                    candidate.sequence_number,
                    candidate.content_sha256,
                )
            )
        except Exception:  # noqa: BLE001 - default display fails closed
            return False

    def _has_exact_pixel_composition_proof(self, candidate: ImageCandidate) -> bool:
        """Verify the immutable receipt and private bytes for one local composition."""
        receipt = self._session.scalar(
            select(PixelCompositionReceipt).where(
                PixelCompositionReceipt.customer_id == candidate.customer_id,
                PixelCompositionReceipt.image_candidate_id == candidate.id,
            )
        )
        if (
            receipt is None
            or receipt.verification_state != "verified"
            or receipt.publishable
            or receipt.result_sha256 != candidate.content_sha256
        ):
            return False
        try:
            return self._content_store.verify_pixel_composition(
                candidate.customer_id,
                candidate.id,
                candidate.content_sha256,
            )
        except Exception:  # noqa: BLE001 - review must fail closed
            return False

    def _load_owned_job_candidate(
        self, customer_id: UUID, job_id: UUID, candidate_id: UUID
    ) -> tuple[ImageOutputJob, ImageCandidate]:
        job = self._repository.get_output_job(customer_id, job_id)
        candidate = self._repository.get_candidate(customer_id, candidate_id)
        if job is None or candidate is None or candidate.output_job_id != job.id:
            raise ImageReviewNotFoundError
        return job, candidate

    def _resolve_existing_request(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        candidate: ImageCandidate,
        *,
        decision: ReviewDecision,
        reason: str | None,
        idempotency_key: str,
    ) -> ImageReviewOutput | None:
        by_key = self._repository.get_review_by_idempotency_key(
            customer_id, idempotency_key
        )
        if by_key is not None:
            if (
                by_key.image_candidate_id != candidate.id
                or by_key.decision != decision
                or by_key.reason != reason
            ):
                raise ImageReviewConflictError("review_idempotency_conflict")
            return self._build_replay(customer_id, job, candidate, by_key)
        if self._repository.get_review(customer_id, candidate.id) is not None:
            raise ImageReviewConflictError("candidate_review_already_decided")
        return None

    def _load_sources(
        self, customer_id: UUID, job: ImageOutputJob
    ) -> tuple[tuple[ProductAssetLink, ...], tuple[ProductAsset, ...]]:
        links = tuple(self._repository.list_asset_links(customer_id, job.id))
        source_assets = tuple(
            self._repository.get_asset(customer_id, link.product_asset_id)
            for link in links
        )
        if not links or any(asset is None for asset in source_assets):
            raise ImageReviewOutcomeUncertainError
        assets = cast(tuple[ProductAsset, ...], source_assets)
        expected_products = job.request_metadata.get("source_product_ids")
        expected_assets = job.request_metadata.get("source_asset_ids")
        if job.request_metadata.get("generation_mode") == "pixel_preserving_isolated":
            raw_sources = job.request_metadata.get("sources")
            if isinstance(raw_sources, list):
                expected_products = [
                    source.get("product_id")
                    for source in raw_sources
                    if isinstance(source, dict)
                ]
                expected_assets = [
                    source.get("asset_id")
                    for source in raw_sources
                    if isinstance(source, dict)
                ]
        if job.output_type == "real_photo":
            expected_products = [job.request_metadata.get("product_id")]
            expected_assets = [job.request_metadata.get("asset_id")]
        if expected_products != [asset.product_id for asset in assets] or [
            str(item) for item in cast(list[object], expected_assets)
        ] != [str(asset.id) for asset in assets]:
            raise ImageReviewOutcomeUncertainError
        return links, assets

    def _build_replay(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        candidate: ImageCandidate,
        review: ImageReview,
    ) -> ImageReviewOutput:
        expected_status = (
            "customer_approved"
            if review.decision == "approved"
            else "customer_rejected"
        )
        suite_item = self._session.scalar(
            select(StyleSuiteRunItem).where(
                StyleSuiteRunItem.customer_id == customer_id,
                StyleSuiteRunItem.output_job_id == job.id,
            )
        )
        suite_rejection_open = (
            suite_item is not None
            and review.decision != "approved"
            and job.status == "awaiting_customer_review"
        )
        if job.status != expected_status and not suite_rejection_open:
            raise ImageReviewOutcomeUncertainError
        source_links, source_assets = self._load_sources(customer_id, job)
        rows = tuple(
            self._repository.list_approved_associations_for_candidate(
                customer_id, candidate.id
            )
        )
        if review.decision == "approved":
            rows_by_source = {row.source_asset_id: row for row in rows}
            if (
                len(rows) != len(source_assets)
                or [row.image_review_id for row in rows] != [review.id] * len(rows)
                or set(rows_by_source) != {asset.id for asset in source_assets}
                or any(row.image_kind != _image_kind(job) for row in rows)
                or any(row.output_job_id != job.id for row in rows)
                or {row.product_asset_link_id for row in rows}
                != {link.id for link in source_links}
            ):
                raise ImageReviewOutcomeUncertainError
            associations = tuple(
                ReviewAssociationOutput(rows_by_source[asset.id], asset)
                for asset in source_assets
            )
        else:
            if rows:
                raise ImageReviewOutcomeUncertainError
            associations = ()
        suite_state = None
        if suite_item is not None:
            run = self._session.scalar(
                select(StyleSuiteRun).where(
                    StyleSuiteRun.customer_id == customer_id,
                    StyleSuiteRun.id == suite_item.run_id,
                )
            )
            suite_state = run.state if run is not None else None
        return ImageReviewOutput(
            job=job,
            candidate=candidate,
            review=review,
            image_kind=_image_kind(job),
            source_assets=source_assets,
            associations=associations,
            created=False,
            suite_state=suite_state,
        )

    def _recover_durable_request(
        self,
        customer_id: UUID,
        job_id: UUID,
        candidate_id: UUID,
        *,
        decision: ReviewDecision,
        reason: str | None,
        idempotency_key: str,
    ) -> ImageReviewOutput | None:
        bind = self._session.get_bind()
        try:
            with Session(bind=bind, expire_on_commit=False) as session:
                service = ImageReviewService(session, self._content_store)
                job, candidate = service._load_owned_job_candidate(
                    customer_id, job_id, candidate_id
                )
                review = service._repository.get_review_by_idempotency_key(
                    customer_id, idempotency_key
                )
                if (
                    review is None
                    or review.image_candidate_id != candidate.id
                    or review.decision != decision
                    or review.reason != reason
                ):
                    return None
                output = service._build_replay(customer_id, job, candidate, review)
                session.expunge_all()
                return output
        except (ImageReviewNotFoundError, ImageReviewOutcomeUncertainError):
            return None

    def _raise_persisted_conflict(
        self, customer_id: UUID, candidate_id: UUID, idempotency_key: str
    ) -> None:
        self._session.expire_all()
        by_key = self._repository.get_review_by_idempotency_key(
            customer_id, idempotency_key
        )
        if by_key is not None and by_key.image_candidate_id != candidate_id:
            raise ImageReviewConflictError("review_idempotency_conflict")
        if self._repository.get_review(customer_id, candidate_id) is not None:
            raise ImageReviewConflictError("candidate_review_already_decided")


def derive_review_candidate_states(
    repository: ProductImageRepository,
    customer_id: UUID,
    job: ImageOutputJob,
    candidates: tuple[ImageCandidate, ...],
) -> dict[UUID, str] | None:
    """Return terminal review-derived candidate states, if the job is terminal."""
    if job.status not in {"customer_approved", "customer_rejected"}:
        return None
    terminal_state = job.status
    states = {candidate.id: "not_selected" for candidate in candidates}
    for candidate in candidates:
        review = repository.get_review(customer_id, candidate.id)
        if review is not None:
            states[candidate.id] = terminal_state
    return states


def _image_kind(job: ImageOutputJob) -> ImageKind:
    return "real_photo" if job.output_type == "real_photo" else "style_image"
