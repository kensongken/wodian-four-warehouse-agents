"""Fail-closed local consumer for producer-confirmed classification handoffs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.contracts.classification_handoff import (
    ClassificationStyleHandoffV1,
    ConfirmedPhotoV1,
    canonical_handoff_bytes,
)
from opc_style.domain.authenticity import DeterministicLocalAuthenticityChecker
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import (
    DeterministicLocalImageGenerator,
    ImageGenerator,
)
from opc_style.models import (
    AuditEvent,
    ClassificationBridgeClaim,
    Customer,
    ImageOutputJob,
    ProductAsset,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
)
from opc_style.providers.openai_images import REAL_MODEL_GENERATION_MODE
from opc_style.repositories import ProductImageRepository
from opc_style.schemas import ProductAssetIntakeRequest
from opc_style.services.assets import ProductAssetIntake, ProductAssetService
from opc_style.services.authenticity import AuthenticityService
from opc_style.services.image_jobs import ImageJobService, ImageSourceSelection
from opc_style.services.local_media_stage import LocalMediaStage
from opc_style.services.real_photos import (
    LocalSyntheticAssetStore,
    RealPhotoOutput,
    RealPhotoOutputService,
)

BridgeStatus = Literal[
    "processing", "needs_confirmation", "pending_real_model", "awaiting_review"
]


@dataclass(frozen=True)
class BridgeResult:
    """One honest local-test handoff outcome; never a publish instruction."""

    handoff_id: UUID
    status: BridgeStatus
    output_job_ids: tuple[UUID, ...]
    created: bool
    suite_preview_url: str | None = None
    publishable: Literal[False] = False


class ClassificationBridgeNotFoundError(Exception):
    """No completed handoff is visible for the requested opaque ID."""


class ClassificationBridgeOutcomeUncertainError(Exception):
    """Receipt consumption happened but a durable outcome cannot be asserted."""


class ClassificationBridgeInProgressError(Exception):
    """Another live request owns this identical durable handoff claim."""


class ClassificationBridgeService:
    """Route confirmed categories without making any new classification decision."""

    def __init__(
        self,
        session: Session,
        media_stage: LocalMediaStage,
        content_store: Callable[[], LocalSyntheticAssetStore],
        *,
        image_generator: ImageGenerator | None = None,
        authenticity_failure_product_id: str | None = None,
    ) -> None:
        self._session = session
        self._media_stage = media_stage
        self._content_store_factory = content_store
        self._content_store: LocalSyntheticAssetStore | None = None
        self._image_generator = image_generator or DeterministicLocalImageGenerator()
        self._authenticity_failure_product_id = authenticity_failure_product_id
        self._owner_token = str(uuid4())

    def accept(self, handoff: ClassificationStyleHandoffV1) -> BridgeResult:
        """Consume attested media once and dispatch only producer-confirmed categories."""
        # The stage lock is an OS flock, so every local process queues through
        # this complete orchestration and a crash releases ownership naturally.
        with self._media_stage.orchestration_lock():
            return self._accept_locked(handoff)

    def _accept_locked(self, handoff: ClassificationStyleHandoffV1) -> BridgeResult:
        """Run the complete handoff while the consumer-owned flock is held."""
        customer_id = _customer_id(handoff.customer_test_id)
        digest = sha256(canonical_handoff_bytes(handoff)).hexdigest()
        replay = self._accepted_replay_or_conflict(customer_id, handoff, digest)
        if replay is not None:
            self._media_stage.release_handoff_receipts(handoff_id=handoff.handoff_id)
            return replay
        # Receipt attestation is deliberately first: malformed media must not
        # create a customer, claim, or audit record in the consumer database.
        paths = self._attest_all_media(handoff)
        self._ensure_customer(customer_id)
        replay = self._claim_or_replay(customer_id, handoff, digest)
        if replay is not None:
            self._media_stage.release_handoff_receipts(handoff_id=handoff.handoff_id)
            return replay

        phase = "receipt_attested"
        output_job_ids: list[UUID] = self._completed_job_ids(customer_id, handoff)
        try:
            self._assert_owner(customer_id, handoff)
            self._validate_job_prerequisites(customer_id, handoff)
            phase = "prerequisites_checked"
            self._media_stage.consume_batch(
                receipts=tuple(
                    (photo.media_receipt_id, photo.photo_id, photo.content_sha256)
                    for photo in handoff.photos
                ),
                handoff_id=handoff.handoff_id,
            )
            phase = "receipt_bound"
            real_assets = self._intake_real_assets(customer_id, handoff, paths)
            phase = "assets_recorded"
            output_job_ids = self._route_categories(
                customer_id,
                handoff,
                real_assets,
                record_job=lambda job_id, step: self._record_step(
                    customer_id, handoff, digest, step, job_id
                ),
            )
            phase = "jobs_completed"
            status = self._derive_status(handoff, output_job_ids)
            result = BridgeResult(
                handoff_id=handoff.handoff_id,
                status=status,
                output_job_ids=tuple(output_job_ids),
                created=True,
                suite_preview_url=(
                    f"/local/style-suite?product_id={handoff.product_id}"
                    f"&source_asset_id={real_assets[handoff.main_photo_id].asset.id}"
                ),
            )
            self._record_result(customer_id, handoff, digest, result)
            self._session.commit()
            self._media_stage.release_handoff_receipts(handoff_id=handoff.handoff_id)
            return result
        except Exception as error:
            self._session.rollback()
            output_job_ids = self._completed_job_ids(customer_id, handoff)
            self._record_cleanup_pending(
                customer_id, handoff, digest, error, phase, output_job_ids
            )
            if phase in {"receipt_attested", "prerequisites_checked"} and isinstance(
                error, DomainRuleError
            ):
                raise
            raise ClassificationBridgeOutcomeUncertainError from error

    def get(self, handoff_id: UUID) -> BridgeResult:
        """Return the append-only completed result without exposing customer data."""
        cleanup_pending: AuditEvent | None = None
        for event in self._session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.event_type.in_(
                    {
                        "classification_bridge.accepted",
                        "classification_bridge.handoff_cleanup_pending",
                    }
                )
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        ):
            payload = event.payload
            if payload.get("handoff_id") != str(handoff_id):
                continue
            if event.event_type == "classification_bridge.accepted":
                result = _result_from_payload(payload, created=False)
                self._media_stage.release_handoff_receipts(handoff_id=handoff_id)
                return BridgeResult(
                    handoff_id=result.handoff_id,
                    status=self._durable_status(event.customer_id, payload, result),
                    output_job_ids=result.output_job_ids,
                    created=False,
                    suite_preview_url=result.suite_preview_url,
                )
            cleanup_pending = event
            break
        if cleanup_pending is not None:
            return _cleanup_pending_result(cleanup_pending.payload)
        raise ClassificationBridgeNotFoundError

    def _claim_or_replay(
        self,
        customer_id: UUID,
        handoff: ClassificationStyleHandoffV1,
        digest: str,
    ) -> BridgeResult | None:
        claim = self._session.scalar(
            select(ClassificationBridgeClaim).where(
                ClassificationBridgeClaim.customer_id == customer_id,
                (ClassificationBridgeClaim.handoff_id == handoff.handoff_id)
                | (
                    ClassificationBridgeClaim.idempotency_key == handoff.idempotency_key
                ),
            )
        )
        if claim is None:
            try:
                self._session.add(
                    ClassificationBridgeClaim(
                        customer_id=customer_id,
                        handoff_id=handoff.handoff_id,
                        idempotency_key=handoff.idempotency_key,
                        handoff_digest=digest,
                        state="started",
                        owner_token=self._owner_token,
                        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    )
                )
                self._record_started(customer_id, handoff, digest)
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                claim = self._session.scalar(
                    select(ClassificationBridgeClaim).where(
                        ClassificationBridgeClaim.customer_id == customer_id,
                        (ClassificationBridgeClaim.handoff_id == handoff.handoff_id)
                        | (
                            ClassificationBridgeClaim.idempotency_key
                            == handoff.idempotency_key
                        ),
                    )
                )
        if claim is not None and (
            claim.handoff_id != handoff.handoff_id
            or claim.idempotency_key != handoff.idempotency_key
            or claim.handoff_digest != digest
        ):
            raise DomainRuleError("classification_handoff_idempotency_conflict")

        if claim is not None:
            if claim.state == "accepted":
                # The append-only accepted event below is the public replay fact.
                pass
            elif (
                claim.owner_token is not None
                and claim.lease_expires_at is not None
                and _as_utc(claim.lease_expires_at) > datetime.now(UTC)
            ):
                raise ClassificationBridgeInProgressError
            else:
                changed = self._session.execute(  # type: ignore[attr-defined]
                    update(ClassificationBridgeClaim)
                    .where(
                        ClassificationBridgeClaim.id == claim.id,
                        ClassificationBridgeClaim.state.in_({"started", "cleanup"}),
                        ClassificationBridgeClaim.owner_token == claim.owner_token,
                        ClassificationBridgeClaim.lease_expires_at
                        == claim.lease_expires_at,
                    )
                    .values(
                        state="started",
                        owner_token=self._owner_token,
                        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                        updated_at=datetime.now(UTC),
                    )
                ).rowcount
                if changed != 1:
                    self._session.rollback()
                    raise ClassificationBridgeInProgressError
                self._session.commit()

        events = self._session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.customer_id == customer_id,
                AuditEvent.event_type.in_(
                    {
                        "classification_bridge.started",
                        "classification_bridge.handoff_cleanup_pending",
                        "classification_bridge.accepted",
                    }
                ),
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        )
        for event in events:
            payload = event.payload
            same_key = payload.get("idempotency_key") == handoff.idempotency_key
            same_handoff = payload.get("handoff_id") == str(handoff.handoff_id)
            if not same_key and not same_handoff:
                continue
            if payload.get("handoff_digest") != digest or not (
                same_key and same_handoff
            ):
                raise DomainRuleError("classification_handoff_idempotency_conflict")
            if event.event_type == "classification_bridge.accepted":
                return _result_from_payload(payload, created=False)
            break
        return None

    def _accepted_replay_or_conflict(
        self, customer_id: UUID, handoff: ClassificationStyleHandoffV1, digest: str
    ) -> BridgeResult | None:
        claim = self._session.scalar(
            select(ClassificationBridgeClaim).where(
                ClassificationBridgeClaim.customer_id == customer_id,
                (ClassificationBridgeClaim.handoff_id == handoff.handoff_id)
                | (
                    ClassificationBridgeClaim.idempotency_key == handoff.idempotency_key
                ),
            )
        )
        if claim is not None and (
            claim.handoff_id != handoff.handoff_id
            or claim.idempotency_key != handoff.idempotency_key
            or claim.handoff_digest != digest
        ):
            raise DomainRuleError("classification_handoff_idempotency_conflict")
        for event in self._session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.customer_id == customer_id,
                AuditEvent.event_type == "classification_bridge.accepted",
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        ):
            payload = event.payload
            same_key = payload.get("idempotency_key") == handoff.idempotency_key
            same_handoff = payload.get("handoff_id") == str(handoff.handoff_id)
            if not same_key and not same_handoff:
                continue
            if (
                not (same_key and same_handoff)
                or payload.get("handoff_digest") != digest
            ):
                raise DomainRuleError("classification_handoff_idempotency_conflict")
            return _result_from_payload(payload, created=False)
        return None

    def _assert_owner(
        self, customer_id: UUID, handoff: ClassificationStyleHandoffV1
    ) -> None:
        changed = self._session.execute(  # type: ignore[attr-defined]
            update(ClassificationBridgeClaim)
            .where(
                ClassificationBridgeClaim.customer_id == customer_id,
                ClassificationBridgeClaim.handoff_id == handoff.handoff_id,
                ClassificationBridgeClaim.state == "started",
                ClassificationBridgeClaim.owner_token == self._owner_token,
            )
            .values(
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                updated_at=datetime.now(UTC),
            )
        ).rowcount
        if changed != 1:
            self._session.rollback()
            raise ClassificationBridgeInProgressError
        self._session.commit()

    def _ensure_customer(self, customer_id: UUID) -> None:
        """Create the local-test owner before its durable idempotency fence."""
        if self._session.get(Customer, customer_id) is None:
            try:
                with self._session.begin_nested():
                    self._session.add(
                        Customer(
                            id=customer_id,
                            display_name="Local classification bridge",
                        )
                    )
                    self._session.flush()
            except IntegrityError:
                # A concurrent first handoff created the same deterministic
                # local customer. Re-query rather than surfacing a 500.
                pass

    def _attest_all_media(
        self, handoff: ClassificationStyleHandoffV1
    ) -> dict[UUID, Path]:
        paths: dict[UUID, Path] = {}
        for photo in handoff.photos:
            path = self._media_stage.resolve_for_handoff(
                receipt_id=photo.media_receipt_id,
                photo_id=photo.photo_id,
                sha256=photo.content_sha256,
                handoff_id=handoff.handoff_id,
            )
            _verify_decoded_media(path, photo)
            paths[photo.photo_id] = path
        return paths

    def _validate_job_prerequisites(
        self, customer_id: UUID, handoff: ClassificationStyleHandoffV1
    ) -> None:
        categories = {photo.confirmed_category for photo in handoff.photos}
        main = next(
            (
                photo
                for photo in handoff.photos
                if photo.photo_id == handoff.main_photo_id
            ),
            None,
        )
        if main is None or main.confirmed_category != "real_photo":
            raise DomainRuleError("classification_handoff_main_real_photo_required")
        if (
            categories
            & {
                "scene_photo",
                "multi_product_photo",
            }
            and self._active_scene_pin(customer_id) is None
        ):
            raise DomainRuleError("local_style_scene_pin_required")
        if "multi_product_photo" in categories:
            known_products = {handoff.product_id}
            known_products.update(
                asset.product_id for asset in self._ready_assets(customer_id)
            )
            if len(known_products) < 2:
                raise DomainRuleError("local_multi_product_sources_required")

    def _intake_real_assets(
        self,
        customer_id: UUID,
        handoff: ClassificationStyleHandoffV1,
        paths: dict[UUID, Path],
    ) -> dict[UUID, ProductAssetIntake]:
        assets: dict[UUID, ProductAssetIntake] = {}
        service = ProductAssetService(self._session)
        for photo in handoff.photos:
            if photo.confirmed_category not in {"real_photo", "wear_photo"}:
                continue
            request = ProductAssetIntakeRequest.model_construct(
                product_id=handoff.product_id,
                photo_id=str(photo.photo_id),
                confirmed=True,
                source_type="synthetic_metadata",
                quality_flags=["classification_local_test"],
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                transform_permission=True,
                content_sha256=photo.content_sha256,
                mime_type=photo.mime_type,
                width=photo.width,
                height=photo.height,
            )
            result = service.intake(customer_id, request)
            self._record_step(
                customer_id,
                handoff,
                sha256(canonical_handoff_bytes(handoff)).hexdigest(),
                "asset_committed",
                result.asset.id,
            )
            self._store().register_original(
                customer_id, result.asset.id, paths[photo.photo_id]
            )
            assets[photo.photo_id] = result
        if not assets:
            raise DomainRuleError("classification_handoff_real_asset_missing")
        return assets

    def _route_categories(
        self,
        customer_id: UUID,
        handoff: ClassificationStyleHandoffV1,
        real_assets: dict[UUID, ProductAssetIntake],
        record_job: Callable[[UUID, str], None],
    ) -> list[UUID]:
        output_job_ids: list[UUID] = []
        primary = real_assets.get(handoff.main_photo_id) or next(
            iter(real_assets.values())
        )
        if self._real_generation_enabled():
            # Classification only registers assets. Generation begins after the
            # customer reviews the complete historical style-suite preview.
            return output_job_ids
        for photo in handoff.photos:
            if photo.confirmed_category == "real_photo":
                output = self._existing_or_real_output(
                    customer_id,
                    handoff,
                    photo,
                    real_assets[photo.photo_id].asset.id,
                )
                output_job_ids.append(output.job.id)
                record_job(output.job.id, "job_committed")
                if output.job.status == "machine_check":
                    self._run_authenticity(customer_id, output.job.id, record_job)
                elif output.job.status in {
                    "awaiting_customer_review",
                    "authenticity_failed",
                }:
                    record_job(output.job.id, "authenticity_already_invoked")
                else:
                    record_job(output.job.id, "terminalized_without_authenticity")
            elif photo.confirmed_category == "scene_photo":
                scene_id, version_id = self._require_scene_pin(customer_id)
                key = f"bridge:{handoff.handoff_id}:scene:{photo.photo_id}"
                scene_job = self._existing_generation_job(customer_id, key)
                if scene_job is None:
                    scene_job = (
                        ImageJobService(
                            self._session,
                            self._store(),
                            self._local_generator(handoff.product_id),
                        )
                        .create(
                            customer_id,
                            output_type="scene",
                            sources=(
                                ImageSourceSelection(
                                    product_id=handoff.product_id,
                                    asset_id=primary.asset.id,
                                ),
                            ),
                            scene_style_id=scene_id,
                            style_card_version_id=version_id,
                            output_ratio="1:1",
                            idempotency_key=key,
                        )
                        .job
                    )
                output_job_ids.append(scene_job.id)
                record_job(scene_job.id, "job_committed")
                if scene_job.status == "machine_check":
                    self._run_authenticity(customer_id, scene_job.id, record_job)
                elif scene_job.status in {
                    "awaiting_customer_review",
                    "authenticity_failed",
                }:
                    record_job(scene_job.id, "authenticity_already_invoked")
                else:
                    record_job(scene_job.id, "terminalized_without_authenticity")
            elif photo.confirmed_category == "multi_product_photo":
                scene_id, version_id = self._require_scene_pin(customer_id)
                sources = self._multi_product_sources(customer_id, primary.asset)
                key = f"bridge:{handoff.handoff_id}:multi:{photo.photo_id}"
                multi_job = self._existing_generation_job(customer_id, key)
                if multi_job is None:
                    multi_job = (
                        ImageJobService(
                            self._session,
                            self._store(),
                            self._local_generator(handoff.product_id),
                        )
                        .create(
                            customer_id,
                            output_type="multi_product",
                            sources=sources,
                            scene_style_id=scene_id,
                            style_card_version_id=version_id,
                            output_ratio="1:1",
                            idempotency_key=key,
                        )
                        .job
                    )
                output_job_ids.append(multi_job.id)
                record_job(multi_job.id, "job_committed")
                if multi_job.status == "machine_check":
                    self._run_authenticity(customer_id, multi_job.id, record_job)
                elif multi_job.status in {
                    "awaiting_customer_review",
                    "authenticity_failed",
                }:
                    record_job(multi_job.id, "authenticity_already_invoked")
                else:
                    record_job(multi_job.id, "terminalized_without_authenticity")
            # wear_photo deliberately creates no job: it remains pending_real_model.
        return output_job_ids

    def _real_generation_enabled(self) -> bool:
        return (
            getattr(self._image_generator, "generation_mode", None)
            == REAL_MODEL_GENERATION_MODE
        )

    def _local_generator(self, product_id: str) -> DeterministicLocalImageGenerator:
        fixture: Literal["pass", "subject_incomplete"] = (
            "subject_incomplete"
            if product_id == self._authenticity_failure_product_id
            else "pass"
        )
        return DeterministicLocalImageGenerator(authenticity_fixture=fixture)

    def _run_authenticity(
        self,
        customer_id: UUID,
        job_id: UUID,
        record_job: Callable[[UUID, str], None],
    ) -> None:
        service = AuthenticityService(
            self._session, self._store(), DeterministicLocalAuthenticityChecker()
        )
        try:
            service.run(customer_id, job_id)
        except Exception:
            record_job(job_id, "authenticity_failed")
            raise
        record_job(job_id, "authenticity_checked")

    def _active_scene_pin(self, customer_id: UUID) -> tuple[UUID, UUID] | None:
        rows = self._session.execute(
            select(SceneStyle.id, StyleCardVersion)
            .join(StyleCard, StyleCard.id == SceneStyle.style_card_id)
            .join(StyleCardVersion, StyleCardVersion.style_card_id == StyleCard.id)
            .where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.active.is_(True),
                StyleCard.customer_id == customer_id,
                StyleCard.kind == "scene",
                StyleCard.active.is_(True),
                StyleCardVersion.customer_id == customer_id,
            )
            .order_by(SceneStyle.created_at, StyleCardVersion.version.desc())
        ).all()
        for scene_id, scene_version in rows:
            raw_main_version_id = scene_version.content.get(
                "main_style_card_version_id"
            )
            try:
                main_version_id = UUID(str(raw_main_version_id))
            except (TypeError, ValueError):
                continue
            main_card = self._session.scalar(
                select(StyleCard)
                .join(StyleCardVersion, StyleCardVersion.style_card_id == StyleCard.id)
                .where(
                    StyleCard.customer_id == customer_id,
                    StyleCard.kind == "main",
                    StyleCard.active.is_(True),
                    StyleCardVersion.customer_id == customer_id,
                    StyleCardVersion.id == main_version_id,
                )
            )
            if main_card is not None:
                return scene_id, scene_version.id
        return None

    def _require_scene_pin(self, customer_id: UUID) -> tuple[UUID, UUID]:
        pin = self._active_scene_pin(customer_id)
        if pin is None:
            raise DomainRuleError("local_style_scene_pin_required")
        return pin

    def _multi_product_sources(
        self, customer_id: UUID, primary: ProductAsset
    ) -> tuple[ImageSourceSelection, ...]:
        selected: dict[str, ProductAsset] = {primary.product_id: primary}
        for asset in self._ready_assets(customer_id):
            if asset.product_id not in selected:
                selected[asset.product_id] = asset
            if len(selected) == 5:
                break
        if len(selected) < 2:
            raise DomainRuleError("local_multi_product_sources_required")
        return tuple(
            ImageSourceSelection(product_id=asset.product_id, asset_id=asset.id)
            for asset in selected.values()
        )

    def _ready_assets(self, customer_id: UUID) -> tuple[ProductAsset, ...]:
        service = ProductAssetService(self._session)
        return tuple(
            asset
            for asset in self._session.scalars(
                select(ProductAsset)
                .where(ProductAsset.customer_id == customer_id)
                .order_by(ProductAsset.created_at, ProductAsset.id)
            )
            if service.readiness(customer_id, asset).generation_ready
        )

    def _derive_status(
        self, handoff: ClassificationStyleHandoffV1, output_job_ids: list[UUID]
    ) -> BridgeStatus:
        if not output_job_ids:
            if self._real_generation_enabled():
                return "needs_confirmation"
            return (
                "pending_real_model"
                if any(
                    photo.confirmed_category == "wear_photo" for photo in handoff.photos
                )
                else "needs_confirmation"
            )
        status = self._status_for_jobs(output_job_ids)
        if (
            status == "awaiting_review"
            and not self._real_generation_enabled()
            and any(
                photo.confirmed_category == "wear_photo" for photo in handoff.photos
            )
        ):
            return "pending_real_model"
        return status

    def _durable_status(
        self,
        customer_id: UUID,
        payload: dict[str, object],
        result: BridgeResult,
    ) -> BridgeStatus:
        jobs = [
            self._session.scalar(
                select(ImageOutputJob).where(
                    ImageOutputJob.id == job_id,
                    ImageOutputJob.customer_id == customer_id,
                )
            )
            for job_id in result.output_job_ids
        ]
        status = self._status_for_jobs(result.output_job_ids, jobs=jobs)
        has_product_style_job = any(
            job is not None and job.output_type == "product_style" for job in jobs
        )
        if (
            status == "awaiting_review"
            and not has_product_style_job
            and payload.get("has_wear_photo") is True
        ):
            return "pending_real_model"
        return status

    def _status_for_jobs(
        self,
        output_job_ids: list[UUID] | tuple[UUID, ...],
        *,
        jobs: list[ImageOutputJob | None] | None = None,
    ) -> BridgeStatus:
        if not output_job_ids:
            return "needs_confirmation"
        jobs = jobs or [
            self._session.get(ImageOutputJob, job_id) for job_id in output_job_ids
        ]
        if any(
            job is None
            or job.status
            in {
                "authenticity_failed",
                "generation_failed",
                "cleanup_pending",
                "cleanup_abandoned",
                "disabled",
                "customer_rejected",
            }
            for job in jobs
        ):
            return "needs_confirmation"
        if all(
            job is not None and job.status == "awaiting_customer_review" for job in jobs
        ):
            return "awaiting_review"
        return "processing"

    def _store(self) -> LocalSyntheticAssetStore:
        if self._content_store is None:
            self._content_store = self._content_store_factory()
        return self._content_store

    def _existing_or_real_output(
        self,
        customer_id: UUID,
        handoff: ClassificationStyleHandoffV1,
        photo: ConfirmedPhotoV1,
        asset_id: UUID,
    ) -> RealPhotoOutput:
        """Resume from a committed job without trusting an old in-memory store."""
        key = f"bridge:{handoff.handoff_id}:real:{photo.photo_id}"
        existing = ProductImageRepository(
            self._session
        ).get_output_job_by_idempotency_key(customer_id, key)
        if existing is not None:
            existing = self._terminalize_unfinished_job(customer_id, existing)
            if existing is not None:
                return RealPhotoOutput(job=existing, candidates=(), created=False)
        return RealPhotoOutputService(self._session, self._store()).create(
            customer_id,
            product_id=handoff.product_id,
            asset_id=asset_id,
            profile="catalog-v1",
            idempotency_key=key,
        )

    def _existing_generation_job(
        self, customer_id: UUID, idempotency_key: str
    ) -> ImageOutputJob | None:
        job = ProductImageRepository(self._session).get_output_job_by_idempotency_key(
            customer_id, idempotency_key
        )
        if job is None:
            return None
        return self._terminalize_unfinished_job(customer_id, job)

    def _terminalize_unfinished_job(
        self, customer_id: UUID, job: ImageOutputJob
    ) -> ImageOutputJob | None:
        """Turn every restart-unsafe state into an honest terminal outcome."""
        targets = {
            "queued": "disabled",
            "generating": "cleanup_abandoned",
            "cleanup_pending": "cleanup_abandoned",
            "machine_check": "disabled",
        }
        target = targets.get(job.status)
        if target is None:
            return job
        ProductImageRepository(self._session).transition_output_job(
            customer_id, job.id, target, failure_reason="bridge_restart_compensated"
        )
        self._session.commit()
        return ProductImageRepository(self._session).get_output_job(customer_id, job.id)

    def _completed_job_ids(
        self, customer_id: UUID, handoff: ClassificationStyleHandoffV1
    ) -> list[UUID]:
        rows = self._session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.customer_id == customer_id,
                AuditEvent.event_type == "classification_bridge.step",
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
        ids: list[UUID] = []
        for event in rows:
            payload = event.payload
            if payload.get("handoff_id") != str(handoff.handoff_id):
                continue
            if payload.get("resource_type") != "output_job":
                continue
            try:
                job_id = UUID(str(payload["resource_id"]))
            except (KeyError, TypeError, ValueError):
                continue
            if job_id not in ids:
                ids.append(job_id)
        for photo in handoff.photos:
            category = photo.confirmed_category
            if category == "wear_photo":
                continue
            prefix = {
                "real_photo": "real",
                "scene_photo": "scene",
                "multi_product_photo": "multi",
            }[category]
            job = ProductImageRepository(
                self._session
            ).get_output_job_by_idempotency_key(
                customer_id, f"bridge:{handoff.handoff_id}:{prefix}:{photo.photo_id}"
            )
            if job is not None and job.id not in ids:
                ids.append(job.id)
        return ids

    def _record_started(
        self,
        customer_id: UUID,
        handoff: ClassificationStyleHandoffV1,
        digest: str,
    ) -> None:
        self._session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type="classification_bridge.started",
                payload={
                    "handoff_id": str(handoff.handoff_id),
                    "handoff_digest": digest,
                    "idempotency_key": handoff.idempotency_key,
                    "publishable": False,
                },
                occurred_at=datetime.now(UTC),
            )
        )

    def _record_step(
        self,
        customer_id: UUID,
        handoff: ClassificationStyleHandoffV1,
        digest: str,
        phase: str,
        resource_id: UUID,
    ) -> None:
        self._assert_owner(customer_id, handoff)
        self._session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type="classification_bridge.step",
                payload={
                    "handoff_id": str(handoff.handoff_id),
                    "handoff_digest": digest,
                    "idempotency_key": handoff.idempotency_key,
                    "phase": phase,
                    "resource_type": "asset"
                    if phase == "asset_committed"
                    else "output_job",
                    "resource_id": str(resource_id),
                    "publishable": False,
                },
                occurred_at=datetime.now(UTC),
            )
        )
        self._session.commit()

    def _record_result(
        self,
        customer_id: UUID,
        handoff: ClassificationStyleHandoffV1,
        digest: str,
        result: BridgeResult,
    ) -> None:
        self._assert_owner(customer_id, handoff)
        claim = self._session.scalar(
            select(ClassificationBridgeClaim).where(
                ClassificationBridgeClaim.customer_id == customer_id,
                ClassificationBridgeClaim.handoff_id == handoff.handoff_id,
            )
        )
        if claim is not None:
            claim.state = "accepted"
            claim.owner_token = None
            claim.lease_expires_at = None
            claim.updated_at = datetime.now(UTC)
        self._session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type="classification_bridge.accepted",
                payload={
                    "handoff_id": str(result.handoff_id),
                    "handoff_digest": digest,
                    "idempotency_key": handoff.idempotency_key,
                    "status": result.status,
                    "output_job_ids": [str(job_id) for job_id in result.output_job_ids],
                    "publishable": False,
                    "source": "classification_local_page",
                    "desensitized_test_only": True,
                    "suite_preview_url": result.suite_preview_url,
                    "has_wear_photo": any(
                        photo.confirmed_category == "wear_photo"
                        for photo in handoff.photos
                    ),
                },
                occurred_at=datetime.now(UTC),
            )
        )

    def _record_cleanup_pending(
        self,
        customer_id: UUID,
        handoff: ClassificationStyleHandoffV1,
        digest: str,
        error: Exception,
        phase: str,
        output_job_ids: list[UUID],
    ) -> None:
        try:
            changed = self._session.execute(  # type: ignore[attr-defined]
                update(ClassificationBridgeClaim)
                .where(
                    ClassificationBridgeClaim.customer_id == customer_id,
                    ClassificationBridgeClaim.handoff_id == handoff.handoff_id,
                    ClassificationBridgeClaim.owner_token == self._owner_token,
                    ClassificationBridgeClaim.state.in_({"started", "cleanup"}),
                )
                .values(
                    state="cleanup",
                    owner_token=None,
                    lease_expires_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            ).rowcount
            if changed != 1:
                self._session.rollback()
                return
            self._session.add(
                AuditEvent(
                    customer_id=customer_id,
                    event_type="classification_bridge.handoff_cleanup_pending",
                    payload={
                        "handoff_id": str(handoff.handoff_id),
                        "handoff_digest": digest,
                        "idempotency_key": handoff.idempotency_key,
                        "reason": _error_code(error),
                        "phase": phase,
                        "output_job_ids": [str(job_id) for job_id in output_job_ids],
                        "receipt_ids": [
                            str(photo.media_receipt_id) for photo in handoff.photos
                        ],
                        "publishable": False,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            self._session.commit()
        except Exception:  # noqa: BLE001 - preserve the original handoff failure
            self._session.rollback()


def _customer_id(customer_test_id: str) -> UUID:
    """Map a test-only opaque customer code without sharing producer storage."""
    return uuid5(NAMESPACE_URL, f"opc-local-classification-bridge:{customer_test_id}")


def _verify_decoded_media(path: Path, photo: ConfirmedPhotoV1) -> None:
    """Re-decode the stage-owned file before a handoff can consume it."""
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            format_name = image.format
            dimensions = image.size
    except (OSError, UnidentifiedImageError, ValueError) as error:
        raise DomainRuleError("local_media_receipt_decode_mismatch") from error
    expected_format = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/heic": "HEIC",
    }[photo.mime_type]
    if format_name != expected_format or dimensions != (photo.width, photo.height):
        raise DomainRuleError("local_media_receipt_decode_mismatch")


def _result_from_payload(payload: dict[str, object], *, created: bool) -> BridgeResult:
    try:
        status: BridgeStatus = payload["status"]  # type: ignore[assignment]
        raw_job_ids = payload["output_job_ids"]
        if not isinstance(raw_job_ids, list):
            raise TypeError("output job IDs must be a list")
        return BridgeResult(
            handoff_id=UUID(str(payload["handoff_id"])),
            status=status,
            output_job_ids=tuple(UUID(str(value)) for value in raw_job_ids),
            created=created,
            suite_preview_url=(
                str(payload["suite_preview_url"])
                if payload.get("suite_preview_url") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ClassificationBridgeNotFoundError from error


def _error_code(error: Exception) -> str:
    return (
        error.code
        if isinstance(error, DomainRuleError)
        else "handoff_persistence_failed"
    )


def _as_utc(value: datetime) -> datetime:
    """SQLite may round-trip timezone values as naive strings."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _cleanup_pending_result(payload: dict[str, object]) -> BridgeResult:
    raw_job_ids = payload.get("output_job_ids", [])
    if not isinstance(raw_job_ids, list):
        raise ClassificationBridgeNotFoundError
    try:
        return BridgeResult(
            handoff_id=UUID(str(payload["handoff_id"])),
            status="needs_confirmation",
            output_job_ids=tuple(UUID(str(value)) for value in raw_job_ids),
            created=False,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ClassificationBridgeNotFoundError from error
