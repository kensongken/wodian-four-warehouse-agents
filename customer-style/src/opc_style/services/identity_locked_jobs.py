"""Ordered, fail-closed identity-locked candidate orchestration."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.generation_readiness import require_paid_style_readiness
from opc_style.domain.identity_background_generator import (
    BACKGROUND_COUNT,
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    BackgroundGenerator,
    BackgroundSafetyChecker,
    GeneratedBackground,
)
from opc_style.domain.identity_compositor import (
    IdentityCompositionError,
    IdentityLockAttestation,
    IdentityLockedComposite,
    Placement,
    attest_identity_lock,
    composite_subject,
)
from opc_style.domain.image_normalizer import NormalizedPayload
from opc_style.domain.subject_seal import (
    PRODUCT_SUBJECT_CANVAS,
    SealedSubject,
    SubjectSegmenter,
)
from opc_style.models import (
    AuditEvent,
    ImageCandidate,
    ImageOutputJob,
    ProductAsset,
    ProductAssetLink,
    ProductSubjectSeal,
    SceneStyle,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.repositories import ProductImageRepository
from opc_style.services.image_jobs import ImageGenerationOutput, _remember_local_store
from opc_style.services.real_photos import (
    AssetContentStore,
    AssetContentStoreError,
    DerivativeTarget,
)
from opc_style.services.subject_seals import (
    ProductSubjectSealService,
    SubjectSealContentStore,
)

IDENTITY_LOCKED_ADAPTER_VERSION = "identity-locked-style-candidate-v1"


class _PngDerivativeStore(AssetContentStore, SubjectSealContentStore, Protocol):
    def reserve_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        *,
        extension: Literal["jpg", "png"] = "jpg",
    ) -> DerivativeTarget: ...


class IdentityLockedJobService:
    """Produce three review candidates while never regenerating product pixels."""

    def __init__(
        self,
        session: Session,
        content_store: _PngDerivativeStore,
        *,
        segmenter: SubjectSegmenter,
        background_generator: BackgroundGenerator,
        background_safety_checker: BackgroundSafetyChecker,
    ) -> None:
        self._session = session
        self._repository = ProductImageRepository(session)
        self._store = content_store
        self._segmenter = segmenter
        self._background_generator = background_generator
        self._safety_checker = background_safety_checker

    def create(
        self,
        customer_id: UUID,
        *,
        asset_id: UUID,
        scene_style_id: UUID,
        style_card_version_id: UUID,
        output_ratio: str,
        idempotency_key: str,
        subject_seal_id: UUID | None = None,
        slot_key: str = "legacy",
        planned_job_id: UUID | None = None,
        mall_lineage: dict[str, Any] | None = None,
    ) -> ImageGenerationOutput:
        fingerprint = _request_fingerprint(
            customer_id,
            asset_id,
            scene_style_id,
            style_card_version_id,
            output_ratio,
            subject_seal_id,
            slot_key,
        )
        existing = self._repository.get_output_job_by_idempotency_key(
            customer_id, idempotency_key
        )
        if existing is not None:
            if existing.request_metadata.get("request_fingerprint") != fingerprint:
                raise DomainRuleError("idempotency_key_conflict")
            return ImageGenerationOutput(
                existing,
                tuple(self._repository.list_candidates(customer_id, existing.id)),
                created=False,
            )

        asset, scene, version, slot_rules = self._require_inputs(
            customer_id,
            asset_id,
            scene_style_id,
            style_card_version_id,
            slot_key=slot_key,
        )
        seal_service = ProductSubjectSealService(self._session, self._store)
        sealed = (
            seal_service.require_reviewed(
                customer_id, subject_seal_id, asset_id=asset.id
            )
            if subject_seal_id is not None
            else seal_service.seal(
                customer_id,
                asset_id=asset.id,
                segmenter=self._segmenter,
                canvas_size=PRODUCT_SUBJECT_CANVAS,
            ).seal
        )
        job_id = planned_job_id or uuid4()
        cleanup_capability = self._store.issue_cleanup_capability(
            customer_id, job_id, (1, 2, 3)
        )
        job = ImageOutputJob(
            id=job_id,
            customer_id=customer_id,
            output_type="product_style",
            style_card_version_id=version.id,
            scene_style_id=scene.id,
            subject_seal_id=sealed.id,
            identity_lock_phase="subject_sealed",
            output_ratio=output_ratio,
            adapter_version=IDENTITY_LOCKED_ADAPTER_VERSION,
            status="generating",
            artifact_store_identity=self._store.store_identity,
            artifact_store_namespace=self._store.store_namespace,
            artifact_store_capability=cleanup_capability,
            expected_artifact_sequences=[1, 2, 3],
            request_metadata={
                **(mall_lineage or {}),
                "schema": "IdentityLockedCompositeV1",
                "request_fingerprint": fingerprint,
                "source_product_ids": [asset.product_id],
                "source_asset_ids": [str(asset.id)],
                "sources": [
                    {"product_id": asset.product_id, "asset_id": str(asset.id)}
                ],
                "style_card_version_id": str(version.id),
                "scene_style_id": str(scene.id),
                "slot_key": slot_key,
                "subject_seal_id": str(sealed.id),
                "generation_mode": (
                    "synthetic_identity_locked_composite"
                    if getattr(self._background_generator, "synthetic_test_only", True)
                    else "real_background_local_composite"
                ),
                "synthetic_test_only": getattr(
                    self._background_generator, "synthetic_test_only", True
                ),
                "publishable": False,
            },
            idempotency_key=idempotency_key,
        )
        self._session.add(job)
        self._session.add(
            ProductAssetLink(
                customer_id=customer_id,
                product_asset_id=asset.id,
                output_job_id=job.id,
                position=1,
            )
        )
        self._session.commit()
        self._session.refresh(job)
        _remember_local_store(job.id, self._store)
        try:
            backgrounds = self._background_generator.generate(
                BackgroundGenerationRequest(
                    style_observations=version.content,
                    slot_rules=slot_rules,
                    output_ratio=output_ratio,
                )
            )
            if len(backgrounds) != BACKGROUND_COUNT:
                raise BackgroundGenerationError(
                    "background_generator_must_return_exactly_three"
                )
            job.identity_lock_phase = "backgrounds_generated"
            safety = [self._safety_checker.check(item) for item in backgrounds]
            if any(result.safe is False for result in safety):
                return self._fail(job, "background_safety_not_passed")
            if any(result.safe is None for result in safety) and all(
                item.synthetic_test_only for item in backgrounds
            ):
                return self._fail(job, "background_safety_not_passed")
            if all(result.safe is True for result in safety):
                job.identity_lock_phase = "backgrounds_safe"
            subject = self._load_subject(customer_id, sealed)
            composites = []
            for background in backgrounds:
                placement = Placement(
                    x=(background.width - subject.width) // 2,
                    y=(background.height - subject.height) // 2,
                )
                composite = composite_subject(background.content, subject, placement)
                attestation = attest_identity_lock(composite, subject)
                if not attestation.protected_pixels_match:
                    return self._fail(job, attestation.code)
                composites.append((background, composite, attestation))
            candidates = self._publish_all(
                customer_id, job, asset, composites, mall_lineage=mall_lineage
            )
            job.identity_lock_phase = "candidates_attested"
            job.status = "machine_check"
            job.failure_reason = None
            self._session.add(
                AuditEvent(
                    customer_id=customer_id,
                    event_type="image_output.identity_lock_completed",
                    payload={
                        "job_id": str(job.id),
                        "subject_seal_id": str(sealed.id),
                        "candidate_count": len(candidates),
                        "publishable": False,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            self._session.commit()
            return ImageGenerationOutput(job, tuple(candidates), created=True)
        except BackgroundGenerationError as error:
            if error.code == "background_generation_uncertain":
                return self._fail(job, error.code, uncertain=True)
            return self._fail(job, error.code)
        except IdentityCompositionError as error:
            return self._fail(job, error.code)
        except Exception:
            self._store.cleanup_derivatives(customer_id, job.id)
            self._session.rollback()
            raise

    def _require_inputs(
        self,
        customer_id: UUID,
        asset_id: UUID,
        scene_style_id: UUID,
        style_card_version_id: UUID,
        *,
        slot_key: str,
    ) -> tuple[ProductAsset, SceneStyle, StyleCardVersion, dict[str, object]]:
        asset = self._repository.get_asset(customer_id, asset_id)
        scene = self._session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.id == scene_style_id,
            )
        )
        version = self._session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == style_card_version_id,
            )
        )
        if asset is None or scene is None or version is None:
            raise DomainRuleError("identity_locked_input_not_found")
        if not scene.active or scene.style_card_id != version.style_card_id:
            raise DomainRuleError("identity_locked_style_not_active")
        slot_rules: dict[str, object] = dict(scene.rules)
        if not getattr(self._background_generator, "synthetic_test_only", True):
            slot_rules = require_identity_locked_paid_style_ready(
                self._session,
                customer_id=customer_id,
                scene=scene,
                version=version,
                slot_key=slot_key,
            )
        return asset, scene, version, slot_rules

    def _load_subject(
        self, customer_id: UUID, seal: ProductSubjectSeal
    ) -> SealedSubject:
        rgba = self._store.read_subject_artifact(
            customer_id, seal.id, "rgba", seal.normalized_subject_sha256
        )
        mask = self._store.read_subject_artifact(
            customer_id, seal.id, "core-mask", seal.mask_sha256
        )
        return SealedSubject(
            rgba_png=rgba,
            rgba_sha256=seal.normalized_subject_sha256,
            core_mask_png=mask,
            core_mask_sha256=seal.mask_sha256,
            width=seal.normalized_width,
            height=seal.normalized_height,
            core_mask_pixel_count=seal.core_mask_pixel_count,
            feather_width_px=seal.feather_width_px,
            adapter_version=seal.segmentation_adapter_version,
            synthetic_test_only=seal.synthetic_test_only,
            publishable=False,
        )

    def _publish_all(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        asset: ProductAsset,
        composites: list[
            tuple[GeneratedBackground, IdentityLockedComposite, IdentityLockAttestation]
        ],
        *,
        mall_lineage: dict[str, Any] | None = None,
    ) -> list[ImageCandidate]:
        candidates: list[ImageCandidate] = []
        try:
            for sequence_number, (background, composite, attestation) in enumerate(
                composites, start=1
            ):
                target = self._store.reserve_derivative(
                    customer_id, job.id, sequence_number, extension="png"
                )
                metadata = {
                    **composite.metadata,
                    **(mall_lineage or {}),
                    "schema": "IdentityLockedCompositeV1",
                    "output_job_id": str(job.id),
                    "sequence_number": sequence_number,
                    "adapter_version": IDENTITY_LOCKED_ADAPTER_VERSION,
                    "generation_mode": "local_identity_locked_composite",
                    "output_type": job.output_type,
                    "output_ratio": job.output_ratio,
                    "source_product_ids": [asset.product_id],
                    "source_asset_ids": [str(asset.id)],
                    "style_card_version_id": str(job.style_card_version_id),
                    "main_style_card_version_id": None,
                    "scene_style_id": str(job.scene_style_id),
                    "synthetic_test_only": composite.synthetic_test_only,
                    "publishable": False,
                    "background_sha256": background.content_sha256,
                    "identity_lock_code": attestation.code,
                    "background_review_state": (
                        "synthetic_fixture_passed"
                        if background.synthetic_test_only
                        else "pending_human_review"
                    ),
                }
                if background.synthetic_test_only:
                    metadata["local_authenticity_fixture"] = {
                        "subject_complete": True,
                        "component_count_preserved": True,
                        "product_shape_preserved": True,
                        "material_preserved": True,
                        "color_shift_within_fixture_tolerance": True,
                        "unexpected_artifacts": [],
                    }
                normalized = self._store.create_derivative(
                    target,
                    NormalizedPayload(
                        content=composite.content,
                        content_sha256=composite.content_sha256,
                        mime_type=composite.mime_type,
                        width=composite.width,
                        height=composite.height,
                        metadata=metadata,
                    ),
                )
                candidate = ImageCandidate(
                    customer_id=customer_id,
                    output_job_id=job.id,
                    sequence_number=sequence_number,
                    identity="identity_locked_style_candidate",
                    artifact_path=str(normalized.output_path),
                    content_sha256=normalized.content_sha256,
                    mime_type="image/png",
                    width=normalized.width,
                    height=normalized.height,
                    adapter_version=IDENTITY_LOCKED_ADAPTER_VERSION,
                    artifact_metadata=metadata,
                )
                candidates.append(candidate)
                self._session.add(candidate)
            for candidate in candidates:
                if not self._store.verify_derivative(
                    customer_id,
                    job.id,
                    candidate.sequence_number,
                    candidate.content_sha256,
                ):
                    raise AssetContentStoreError("asset_derivative_attestation_failed")
            return candidates
        except Exception:
            self._store.cleanup_derivatives(customer_id, job.id)
            raise

    def _fail(
        self,
        job: ImageOutputJob,
        reason: str,
        *,
        uncertain: bool = False,
    ) -> ImageGenerationOutput:
        self._store.cleanup_derivatives(job.customer_id, job.id)
        job.status = "generation_uncertain" if uncertain else "generation_failed"
        job.identity_lock_phase = job.status
        job.failure_reason = reason
        self._session.commit()
        return ImageGenerationOutput(job, (), created=True)


def _request_fingerprint(
    customer_id: UUID,
    asset_id: UUID,
    scene_style_id: UUID,
    style_card_version_id: UUID,
    output_ratio: str,
    subject_seal_id: UUID | None,
    slot_key: str,
) -> str:
    canonical = json.dumps(
        {
            "customer_id": str(customer_id),
            "asset_id": str(asset_id),
            "scene_style_id": str(scene_style_id),
            "style_card_version_id": str(style_card_version_id),
            "output_ratio": output_ratio,
            "subject_seal_id": str(subject_seal_id) if subject_seal_id else None,
            "slot_key": slot_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def require_identity_locked_paid_style_ready(
    session: Session,
    *,
    customer_id: UUID,
    scene: SceneStyle,
    version: StyleCardVersion,
    slot_key: str,
) -> dict[str, object]:
    """Resolve the sealed slot and enforce real evidence before quoting money."""

    slot = session.scalar(
        select(StyleTemplateSlot).where(
            StyleTemplateSlot.customer_id == customer_id,
            StyleTemplateSlot.scene_style_id == scene.id,
            StyleTemplateSlot.style_card_version_id == version.id,
            StyleTemplateSlot.slot_key == slot_key,
            StyleTemplateSlot.active.is_(True),
        )
    )
    if slot is None:
        raise DomainRuleError("identity_locked_slot_rules_incomplete")
    active_sample_count = (
        session.scalar(
            select(func.count(StyleCardSampleLink.id))
            .join(
                StyleSample,
                (StyleSample.customer_id == StyleCardSampleLink.customer_id)
                & (StyleSample.id == StyleCardSampleLink.style_sample_id),
            )
            .where(
                StyleCardSampleLink.customer_id == customer_id,
                StyleCardSampleLink.style_card_version_id == version.id,
                StyleSample.withdrawn_at.is_(None),
                StyleSample.content_sha256.is_not(None),
                StyleSample.mime_type.is_not(None),
                StyleSample.width.is_not(None),
                StyleSample.height.is_not(None),
            )
        )
        or 0
    )
    rules = dict(slot.rules)
    require_paid_style_readiness(
        style_content=version.content,
        active_sample_count=active_sample_count,
        slot_rules=rules,
    )
    return rules
