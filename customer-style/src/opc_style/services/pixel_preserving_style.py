"""Fail-closed orchestration for product-blind backgrounds and local composition."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.background_generator import (
    BackgroundGenerationError,
    BackgroundGenerationPreview,
    BackgroundGenerationRequest,
    BackgroundGenerator,
    BackgroundStyleReference,
    validate_background_generation_request,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.pixel_compositor import (
    PixelComposition,
    PixelCompositionRecommendation,
    PixelCompositor,
)
from opc_style.domain.product_foreground import ForegroundTransform
from opc_style.models import (
    ImageCandidate,
    ImageOutputJob,
    PixelBackgroundRun,
    PixelCompositionReceipt,
    ProductForegroundVersion,
    ProductIdentityCardVersion,
    StyleSuiteRun,
)
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError
from opc_style.services.style_reference_packs import StyleReferencePackService
from opc_style.settings import RealBackgroundProviderSettings


@dataclass(frozen=True)
class PixelBackgroundOutcome:
    id: UUID
    dispatch_state: str
    outbound_attempts: int
    result_sha256: str | None
    review_state: str
    failure_code: str | None
    output_width: int
    output_height: int
    created: bool


@dataclass(frozen=True)
class PixelCompositionOutcome:
    candidate_id: UUID
    receipt_id: UUID
    result_sha256: str
    publishable: Literal[False] = False


class PixelPreservingStyleService:
    """Keep paid background generation separate from local product pixels."""

    def __init__(
        self,
        session: Session,
        *,
        content_store: AssetContentStore,
        settings: RealBackgroundProviderSettings,
        generator: BackgroundGenerator | None,
        compositor: PixelCompositor | None = None,
    ) -> None:
        self._session = session
        self._content_store = content_store
        self._settings = settings
        self._generator = generator
        self._compositor = compositor or PixelCompositor()

    def preview(
        self,
        customer_id: UUID,
        *,
        style_suite_run_id: UUID,
        style_suite_run_item_id: UUID | None = None,
        foreground_version_id: UUID,
        style_reference_pack_id: UUID,
        slot_rules: dict[str, object],
        style_rules: dict[str, object],
        output_size: tuple[int, int] = (1024, 1024),
    ) -> BackgroundGenerationPreview:
        if not self._settings.enabled or self._generator is None:
            raise DomainRuleError("real_background_provider_disabled")
        suite, foreground = self._scope(
            customer_id, style_suite_run_id, foreground_version_id
        )
        pack = StyleReferencePackService(self._session).get(
            customer_id, style_reference_pack_id
        )
        manifest: dict[str, object] = {
            "version": "pixel-background-manifest-v1",
            "customer_id": str(customer_id),
            "style_suite_run_id": str(suite.id),
            "foreground_version_id": str(foreground.id),
            "foreground_manifest_sha256": foreground.manifest_sha256,
            "product_category": self._product_category(suite),
            "style_reference_pack_id": str(pack.id),
            "style_reference_manifest_sha256": pack.manifest_sha256,
            "style_references": [
                {
                    "sample_id": str(reference.sample_id),
                    "content_sha256": reference.content_sha256,
                    "mime_type": reference.mime_type,
                }
                for reference in pack.references
            ],
            "slot_rules": slot_rules,
            "style_rules": style_rules,
            "output_size": list(output_size),
            "model": self._settings.model,
            "background_adapter_version": getattr(
                self._generator, "adapter_version", "unavailable"
            ),
            "maximum_usd": str(self._settings.max_run_usd),
            "provider_image_count": len(pack.references),
            "publishable": False,
        }
        if style_suite_run_item_id is not None:
            manifest["style_suite_run_item_id"] = str(style_suite_run_item_id)
        digest = self._manifest_sha256(manifest)
        return BackgroundGenerationPreview(
            customer_id=customer_id,
            style_suite_run_id=suite.id,
            foreground_version_id=foreground.id,
            style_reference_pack_id=pack.id,
            authorized_manifest_sha256=digest,
            authorized_manifest=manifest,
            model=self._settings.model,
            maximum_usd=self._settings.max_run_usd,
            provider_image_count=len(pack.references),
        )

    def execute_background(
        self,
        customer_id: UUID,
        preview: BackgroundGenerationPreview,
        *,
        idempotency_key: str,
        confirmed_maximum_usd: Decimal,
    ) -> PixelBackgroundOutcome:
        if preview.customer_id != customer_id:
            raise DomainRuleError("pixel_background_scope_mismatch")
        existing = self._session.scalar(
            select(PixelBackgroundRun).where(
                PixelBackgroundRun.customer_id == customer_id,
                PixelBackgroundRun.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return self._outcome(existing, created=False)
        if (
            confirmed_maximum_usd != preview.maximum_usd
            or confirmed_maximum_usd != self._settings.max_run_usd
            or preview.model != self._settings.model
        ):
            raise DomainRuleError("pixel_background_budget_mismatch")
        current = self._rebuild_preview(preview)
        if (
            current.authorized_manifest_sha256 != preview.authorized_manifest_sha256
            or current.authorized_manifest != preview.authorized_manifest
        ):
            raise DomainRuleError("pixel_background_manifest_changed")
        run = PixelBackgroundRun(
            id=uuid4(),
            customer_id=customer_id,
            style_suite_run_id=preview.style_suite_run_id,
            foreground_version_id=preview.foreground_version_id,
            authorized_manifest_sha256=preview.authorized_manifest_sha256,
            authorized_manifest=preview.authorized_manifest,
            idempotency_key=idempotency_key,
            model=preview.model,
            maximum_usd=preview.maximum_usd,
            dispatch_state="reserved",
            outbound_attempts=0,
            review_state="awaiting_review",
        )
        self._session.add(run)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            replay = self._session.scalar(
                select(PixelBackgroundRun).where(
                    PixelBackgroundRun.customer_id == customer_id,
                    PixelBackgroundRun.idempotency_key == idempotency_key,
                )
            )
            if replay is None:
                raise
            return self._outcome(replay, created=False)

        provider_request = self._provider_request(current)
        try:
            validate_background_generation_request(provider_request)
        except BackgroundGenerationError as error:
            run.failure_code = error.code
            run.updated_at = datetime.now(UTC)
            self._session.commit()
            raise DomainRuleError(error.code) from error
        run.outbound_attempts = 1
        run.updated_at = datetime.now(UTC)
        self._session.commit()
        try:
            assert self._generator is not None
            generated = self._generator.generate(provider_request)
            self._content_store.persist_pixel_background(
                customer_id, run.id, generated.content, generated.content_sha256
            )
            if not self._content_store.verify_pixel_background(
                customer_id, run.id, generated.content_sha256
            ):
                raise AssetContentStoreError("pixel_artifact_unavailable")
        except BackgroundGenerationError as error:
            run.dispatch_state = (
                "dispatch_uncertain"
                if error.code == "background_dispatch_uncertain"
                else "provider_responded"
            )
            run.failure_code = error.code
            run.updated_at = datetime.now(UTC)
            self._session.commit()
            raise DomainRuleError(error.code) from error
        except AssetContentStoreError as error:
            run.dispatch_state = "provider_responded"
            run.failure_code = "background_storage_attestation_failed"
            run.updated_at = datetime.now(UTC)
            self._session.commit()
            raise DomainRuleError("background_storage_attestation_failed") from error
        run.dispatch_state = "provider_responded"
        run.result_sha256 = generated.content_sha256
        run.updated_at = datetime.now(UTC)
        self._session.commit()
        return self._outcome(run, created=True)

    def confirm_background(
        self,
        customer_id: UUID,
        run_id: UUID,
        *,
        decision: Literal["confirmed", "rejected"],
    ) -> PixelBackgroundOutcome:
        row = self._run(customer_id, run_id)
        if row.result_sha256 is None or row.dispatch_state != "provider_responded":
            raise DomainRuleError("pixel_background_not_reviewable")
        if row.review_state != "awaiting_review":
            if row.review_state == decision:
                return self._outcome(row, created=False)
            raise DomainRuleError("pixel_background_decision_immutable")
        if not self._content_store.verify_pixel_background(
            customer_id, row.id, row.result_sha256
        ):
            raise DomainRuleError("background_storage_attestation_failed")
        row.review_state = decision
        row.reviewed_at = datetime.now(UTC)
        row.updated_at = row.reviewed_at
        self._session.commit()
        return self._outcome(row, created=True)

    def compose(
        self,
        customer_id: UUID,
        run_id: UUID,
        *,
        output_job_id: UUID,
        transform: ForegroundTransform,
        output_size: tuple[int, int],
    ) -> PixelCompositionOutcome:
        recommendation = self.recommend_composition(customer_id, run_id)
        if not recommendation.save_allowed:
            if recommendation.category_calibration_state == "required":
                raise DomainRuleError("pixel_composition_category_calibration_required")
            if "foreground_scale_insufficient" in recommendation.reason_codes:
                raise DomainRuleError("pixel_composition_scale_insufficient")
            raise DomainRuleError("pixel_background_tabletop_insufficient")
        composition = self.preview_composition(
            customer_id,
            run_id,
            transform=transform,
            output_size=output_size,
        )
        run = self._run(customer_id, run_id)
        foreground = self._session.scalar(
            select(ProductForegroundVersion).where(
                ProductForegroundVersion.customer_id == customer_id,
                ProductForegroundVersion.id == run.foreground_version_id,
                ProductForegroundVersion.state == "confirmed",
            )
        )
        job = self._session.scalar(
            select(ImageOutputJob).where(
                ImageOutputJob.customer_id == customer_id,
                ImageOutputJob.id == output_job_id,
            )
        )
        if foreground is None or job is None:
            raise DomainRuleError("pixel_composition_scope_mismatch")
        sequence = (
            self._session.scalar(
                select(func.max(ImageCandidate.sequence_number)).where(
                    ImageCandidate.customer_id == customer_id,
                    ImageCandidate.output_job_id == job.id,
                )
            )
            or 0
        ) + 1
        if sequence > 3:
            raise DomainRuleError("pixel_composition_candidate_limit")
        candidate_id = uuid4()
        self._content_store.persist_pixel_composition(
            customer_id, candidate_id, composition.png_bytes, composition.output_sha256
        )
        candidate = ImageCandidate(
            id=candidate_id,
            customer_id=customer_id,
            output_job_id=job.id,
            sequence_number=sequence,
            identity="pixel_preserving_composition",
            artifact_path=f"private:pixel-composition:{candidate_id}",
            content_sha256=composition.output_sha256,
            mime_type="image/png",
            width=composition.width,
            height=composition.height,
            adapter_version=self._compositor.compositor_version,
            artifact_metadata={
                "generation_mode": "pixel_preserving_isolated",
                "publishable": False,
                "background_run_id": str(run.id),
                "foreground_version_id": str(foreground.id),
            },
        )
        receipt = PixelCompositionReceipt(
            id=uuid4(),
            customer_id=customer_id,
            foreground_version_id=foreground.id,
            background_run_id=run.id,
            image_candidate_id=candidate.id,
            transform_payload=composition.transform_payload,
            compositor_version=self._compositor.compositor_version,
            verification_state="verified",
            foreground_sha256=foreground.foreground_sha256,
            background_sha256=run.result_sha256,
            result_sha256=composition.output_sha256,
            transformed_alpha_sha256=composition.transformed_alpha_sha256,
            foreground_rgb_sha256=composition.foreground_rgb_sha256,
            publishable=False,
        )
        self._session.add_all((candidate, receipt))
        self._session.commit()
        if not self._content_store.verify_pixel_composition(
            customer_id, candidate.id, composition.output_sha256
        ):
            raise DomainRuleError("pixel_composition_storage_attestation_failed")
        return PixelCompositionOutcome(
            candidate_id=candidate.id,
            receipt_id=receipt.id,
            result_sha256=composition.output_sha256,
        )

    def preview_composition(
        self,
        customer_id: UUID,
        run_id: UUID,
        *,
        transform: ForegroundTransform,
        output_size: tuple[int, int],
    ) -> PixelComposition:
        """Render and verify a local preview without consuming a candidate slot."""
        run = self._run(customer_id, run_id)
        if run.review_state != "confirmed" or run.result_sha256 is None:
            raise DomainRuleError("confirmed_pixel_background_required")
        if output_size != self._authorized_output_size(run):
            raise DomainRuleError("pixel_composition_size_mismatch")
        foreground = self._session.scalar(
            select(ProductForegroundVersion).where(
                ProductForegroundVersion.customer_id == customer_id,
                ProductForegroundVersion.id == run.foreground_version_id,
                ProductForegroundVersion.state == "confirmed",
            )
        )
        if foreground is None:
            raise DomainRuleError("pixel_composition_scope_mismatch")
        if (
            foreground.pipeline_manifest_sha256 is None
            or foreground.stage_receipts_sha256 is None
        ):
            raise DomainRuleError("foreground_pipeline_evidence_required")
        try:
            foreground_png = self._content_store.read_product_foreground(
                customer_id, foreground.id, foreground.foreground_sha256
            )
            mask_png = self._content_store.read_product_mask(
                customer_id, foreground.id, foreground.mask_sha256
            )
            background = self._content_store.read_pixel_background(
                customer_id, run.id, run.result_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("pixel_composition_artifact_unavailable") from error
        composition = self._compositor.compose(
            foreground_png, mask_png, background, transform, output_size
        )
        self._compositor.verify(composition, foreground_png, mask_png, background)
        return composition

    def recommend_composition(
        self, customer_id: UUID, run_id: UUID
    ) -> PixelCompositionRecommendation:
        """Return a read-only local placement recommendation for a confirmed run."""
        run = self._run(customer_id, run_id)
        if run.review_state != "confirmed" or run.result_sha256 is None:
            raise DomainRuleError("confirmed_pixel_background_required")
        foreground = self._session.scalar(
            select(ProductForegroundVersion).where(
                ProductForegroundVersion.customer_id == customer_id,
                ProductForegroundVersion.id == run.foreground_version_id,
                ProductForegroundVersion.state == "confirmed",
            )
        )
        if foreground is None:
            raise DomainRuleError("pixel_composition_scope_mismatch")
        try:
            foreground_png = self._content_store.read_product_foreground(
                customer_id, foreground.id, foreground.foreground_sha256
            )
            mask_png = self._content_store.read_product_mask(
                customer_id, foreground.id, foreground.mask_sha256
            )
            background = self._content_store.read_pixel_background(
                customer_id, run.id, run.result_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("pixel_composition_artifact_unavailable") from error
        return self._compositor.recommend(
            foreground_png,
            mask_png,
            background,
            self._authorized_output_size(run),
            product_category=self._manifest_product_category(run),
        )

    def _product_category(self, suite: StyleSuiteRun) -> str:
        card_id = suite.product_identity_card_version_id
        if card_id is None:
            return "legacy_unspecified"
        card = self._session.scalar(
            select(ProductIdentityCardVersion).where(
                ProductIdentityCardVersion.customer_id == suite.customer_id,
                ProductIdentityCardVersion.id == card_id,
            )
        )
        if card is None:
            return "legacy_unspecified"
        category = card.card_payload.get("category")
        value = category.get("value") if isinstance(category, dict) else None
        if not isinstance(value, str) or not value.strip():
            return "legacy_unspecified"
        return value.strip().lower()

    @staticmethod
    def _manifest_product_category(run: PixelBackgroundRun) -> str:
        value = run.authorized_manifest.get("product_category")
        if not isinstance(value, str) or not value.strip():
            return "legacy_unspecified"
        return value.strip().lower()

    def _rebuild_preview(
        self, preview: BackgroundGenerationPreview
    ) -> BackgroundGenerationPreview:
        manifest = preview.authorized_manifest
        raw_item_id = manifest.get("style_suite_run_item_id")
        try:
            item_id = UUID(raw_item_id) if isinstance(raw_item_id, str) else None
        except ValueError as error:
            raise DomainRuleError("pixel_background_manifest_invalid") from error
        slot_rules = manifest.get("slot_rules")
        style_rules = manifest.get("style_rules")
        output_size = manifest.get("output_size")
        if (
            not isinstance(slot_rules, dict)
            or not isinstance(style_rules, dict)
            or not isinstance(output_size, list)
            or len(output_size) != 2
            or not all(isinstance(value, int) for value in output_size)
        ):
            raise DomainRuleError("pixel_background_manifest_invalid")
        return self.preview(
            preview.customer_id,
            style_suite_run_id=preview.style_suite_run_id,
            style_suite_run_item_id=item_id,
            foreground_version_id=preview.foreground_version_id,
            style_reference_pack_id=preview.style_reference_pack_id,
            slot_rules=cast(dict[str, object], slot_rules),
            style_rules=cast(dict[str, object], style_rules),
            output_size=(output_size[0], output_size[1]),
        )

    def _provider_request(
        self, preview: BackgroundGenerationPreview
    ) -> BackgroundGenerationRequest:
        pack = StyleReferencePackService(self._session).get(
            preview.customer_id, preview.style_reference_pack_id
        )
        references: list[BackgroundStyleReference] = []
        for reference in pack.references:
            try:
                content = self._content_store.read_style_sample_content(
                    preview.customer_id,
                    reference.sample_id,
                    reference.content_sha256,
                )
            except AssetContentStoreError as error:
                raise DomainRuleError("style_reference_pack_unavailable") from error
            references.append(
                BackgroundStyleReference(
                    sample_id=reference.sample_id,
                    content=content,
                    content_sha256=reference.content_sha256,
                    mime_type=reference.mime_type,
                )
            )
        manifest = preview.authorized_manifest
        size = cast(list[int], manifest["output_size"])
        return BackgroundGenerationRequest(
            style_reference_pack_id=pack.id,
            style_reference_manifest_sha256=pack.manifest_sha256,
            references=tuple(references),
            slot_rules=cast(dict[str, object], manifest["slot_rules"]),
            style_rules=cast(dict[str, object], manifest["style_rules"]),
            output_size=(size[0], size[1]),
        )

    def _scope(
        self, customer_id: UUID, style_suite_run_id: UUID, foreground_version_id: UUID
    ) -> tuple[StyleSuiteRun, ProductForegroundVersion]:
        suite = self._session.scalar(
            select(StyleSuiteRun).where(
                StyleSuiteRun.customer_id == customer_id,
                StyleSuiteRun.id == style_suite_run_id,
            )
        )
        foreground = self._session.scalar(
            select(ProductForegroundVersion).where(
                ProductForegroundVersion.customer_id == customer_id,
                ProductForegroundVersion.id == foreground_version_id,
                ProductForegroundVersion.state == "confirmed",
            )
        )
        if (
            suite is None
            or foreground is None
            or foreground.product_asset_id != suite.source_asset_id
            or foreground.product_id != suite.product_id
            or foreground.source_content_sha256 != suite.source_content_sha256
        ):
            raise DomainRuleError("pixel_background_scope_mismatch")
        return suite, foreground

    def _run(self, customer_id: UUID, run_id: UUID) -> PixelBackgroundRun:
        row = self._session.scalar(
            select(PixelBackgroundRun).where(
                PixelBackgroundRun.customer_id == customer_id,
                PixelBackgroundRun.id == run_id,
            )
        )
        if row is None:
            raise DomainRuleError("pixel_background_unavailable")
        return row

    @staticmethod
    def _manifest_sha256(manifest: dict[str, object]) -> str:
        canonical = json.dumps(
            manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        return sha256(canonical).hexdigest()

    @staticmethod
    def _authorized_output_size(row: PixelBackgroundRun) -> tuple[int, int]:
        raw = row.authorized_manifest.get("output_size")
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) for value in raw
            )
            or any(value <= 0 for value in raw)
        ):
            raise DomainRuleError("pixel_background_manifest_invalid")
        return raw[0], raw[1]

    @classmethod
    def _outcome(
        cls, row: PixelBackgroundRun, *, created: bool
    ) -> PixelBackgroundOutcome:
        output_width, output_height = cls._authorized_output_size(row)
        return PixelBackgroundOutcome(
            id=row.id,
            dispatch_state=row.dispatch_state,
            outbound_attempts=row.outbound_attempts,
            result_sha256=row.result_sha256,
            review_state=row.review_state,
            failure_code=row.failure_code,
            output_width=output_width,
            output_height=output_height,
            created=created,
        )
