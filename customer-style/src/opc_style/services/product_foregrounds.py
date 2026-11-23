"""Preview, decide, recover, and privately read product foreground versions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4, uuid5

from PIL import Image, ImageChops, ImageEnhance, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import RiskSignals, RiskVerdict
from opc_style.domain.product_foreground import (
    EditedForeground,
    ExtractedForeground,
    ForegroundExtractionRequest,
    ForegroundQuality,
    ProductForegroundExtractor,
    compute_mask_edit_metrics,
    foreground_manifest_evidence_sha256,
    foreground_manifest_sha256,
    foreground_source_quality_flags,
    mask_edit_manifest_sha256,
    mask_pixels_sha256,
    normalize_edited_mask,
    rebuild_foreground_from_original,
    stage_receipts_sha256,
    validate_extracted_foreground,
)
from opc_style.models import (
    ForegroundShadowDecision,
    ProductAsset,
    ProductForegroundDisagreementEvidence,
    ProductForegroundMaskRevision,
    ProductForegroundVersion,
    ProductIdentityCardVersion,
)
from opc_style.services.foreground_risk_gate import (
    DisagreementClassification,
    RiskThresholds,
    ShadowRiskAssessment,
    StageDisagreementProfile,
    assess_shadow_risk,
    compare_extraction_stages,
    profile_extraction_stage_disagreement,
)
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError


@dataclass(frozen=True)
class ProductForegroundOutcome:
    id: UUID
    asset_id: UUID
    product_id: str
    version: int
    source_content_sha256: str
    foreground_sha256: str
    mask_sha256: str
    width: int
    height: int
    extractor_version: str
    manifest_sha256: str
    state: Literal["proposed", "confirmed", "rejected"]
    quality_flags: tuple[str, ...]
    shadow_verdict: RiskVerdict | None
    shadow_reason_codes: tuple[str, ...]
    shadow_model_manifest_sha256: str | None
    pipeline_manifest_sha256: str | None
    stage_receipts_sha256: str | None
    disagreement_available: bool
    disagreement_classification: DisagreementClassification | None
    disagreement_ratio: float | None
    coarse_only_ratio: float | None
    refined_only_ratio: float | None
    coarse_only_detached_ratio: float | None
    created: bool


@dataclass(frozen=True)
class MaskEditOperationCounts:
    magic_wand_count: int
    erase_stroke_count: int
    restore_stroke_count: int


class ProductForegroundShadowEvaluator(Protocol):
    """Optional qualified second opinion; it never performs a human decision."""

    model_manifest_sha256: str
    thresholds: RiskThresholds

    def evaluate(
        self,
        request: ForegroundExtractionRequest,
        baseline: ExtractedForeground,
        quality: ForegroundQuality,
    ) -> RiskSignals: ...


class ProductForegroundService:
    """Seal local extraction evidence before it can enter paid generation."""

    def __init__(
        self,
        session: Session,
        *,
        content_store: AssetContentStore,
        extractor: ProductForegroundExtractor | None,
        shadow_evaluator: ProductForegroundShadowEvaluator | None = None,
    ) -> None:
        self._session = session
        self._content_store = content_store
        self._extractor = extractor
        self._shadow_evaluator = shadow_evaluator

    def preview(self, customer_id: UUID, asset_id: UUID) -> ProductForegroundOutcome:
        asset = self._asset(customer_id, asset_id)
        if self._extractor is None:
            raise DomainRuleError("foreground_extractor_unavailable")
        try:
            source = self._content_store.read_original(
                customer_id, asset.id, asset.content_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("foreground_asset_unavailable") from error
        request = ForegroundExtractionRequest(
            customer_id=customer_id,
            asset_id=asset.id,
            product_id=asset.product_id,
            source_content=source,
            source_content_sha256=asset.content_sha256,
            mime_type=asset.mime_type,
            width=asset.width,
            height=asset.height,
        )
        result = self._extractor.extract(request)
        quality = validate_extracted_foreground(result)
        shadow = self._evaluate_shadow(request, result, quality)
        manifest_sha256 = foreground_manifest_sha256(request, result)
        next_version = (
            self._session.scalar(
                select(func.max(ProductForegroundVersion.version)).where(
                    ProductForegroundVersion.customer_id == customer_id,
                    ProductForegroundVersion.product_asset_id == asset.id,
                )
            )
            or 0
        ) + 1
        version_id = uuid4()
        foreground_persisted = False
        mask_persisted = False
        coarse_mask_persisted = False
        try:
            self._content_store.persist_product_foreground(
                customer_id,
                version_id,
                result.foreground_png,
                result.foreground_sha256,
            )
            foreground_persisted = True
            self._content_store.persist_product_mask(
                customer_id, version_id, result.mask_png, result.mask_sha256
            )
            mask_persisted = True
            row = ProductForegroundVersion(
                id=version_id,
                customer_id=customer_id,
                product_asset_id=asset.id,
                product_id=asset.product_id,
                version=next_version,
                source_content_sha256=asset.content_sha256,
                foreground_sha256=result.foreground_sha256,
                mask_sha256=result.mask_sha256,
                width=result.width,
                height=result.height,
                extractor_version=result.extractor_version,
                manifest_sha256=manifest_sha256,
                pipeline_manifest_sha256=result.pipeline_manifest_sha256,
                stage_receipts_sha256=(
                    stage_receipts_sha256(result.stage_receipts)
                    if result.stage_receipts
                    else None
                ),
                state="proposed",
                quality_flags=(
                    [
                        *foreground_source_quality_flags(quality),
                        *(["touches_edge"] if quality.touches_edge else []),
                        *(
                            [f"shadow:{shadow[0].verdict}"]
                            if shadow is not None
                            else []
                        ),
                    ]
                ),
            )
            self._session.add(row)
            self._session.flush()
            if result.coarse_mask_png is not None:
                coarse_digest = (
                    result.coarse_mask_sha256
                    or sha256(result.coarse_mask_png).hexdigest()
                )
                self._content_store.persist_product_mask(
                    customer_id,
                    self._coarse_mask_storage_id(version_id),
                    result.coarse_mask_png,
                    coarse_digest,
                )
                coarse_mask_persisted = True
                self._session.add(
                    ProductForegroundDisagreementEvidence(
                        customer_id=customer_id,
                        foreground_version_id=row.id,
                        coarse_mask_sha256=coarse_digest,
                    )
                )
            if shadow is not None:
                assessment, model_digest, evidence_complete = shadow
                self._session.add(
                    ForegroundShadowDecision(
                        customer_id=customer_id,
                        run_id=None,
                        sample_result_id=None,
                        foreground_version_id=row.id,
                        model_manifest_sha256=model_digest,
                        config_sha256=assessment.config_sha256,
                        verdict=assessment.verdict,
                        reason_codes=list(assessment.reason_codes),
                        evidence_complete=evidence_complete,
                        pipeline_manifest_sha256=result.pipeline_manifest_sha256,
                        stage_receipts_sha256=(
                            stage_receipts_sha256(result.stage_receipts)
                            if result.stage_receipts
                            else None
                        ),
                        risk_parameters_sha256=(
                            assessment.config_sha256
                            if result.pipeline_manifest_sha256 is not None
                            else None
                        ),
                    )
                )
            self._session.commit()
        except Exception:
            self._session.rollback()
            if coarse_mask_persisted and result.coarse_mask_png is not None:
                self._discard_mask(
                    customer_id,
                    self._coarse_mask_storage_id(version_id),
                    result.coarse_mask_sha256
                    or sha256(result.coarse_mask_png).hexdigest(),
                )
            if mask_persisted:
                self._discard_mask(customer_id, version_id, result.mask_sha256)
            if foreground_persisted:
                self._discard_foreground(
                    customer_id, version_id, result.foreground_sha256
                )
            raise
        return self._outcome(row, quality=quality, created=True)

    def decide(
        self,
        customer_id: UUID,
        asset_id: UUID,
        *,
        version_id: UUID,
        manifest_sha256: str,
        confirmation_key: str,
        decision: str,
        confirmed: bool,
    ) -> ProductForegroundOutcome:
        if (decision, confirmed) not in {("confirmed", True), ("rejected", False)}:
            raise DomainRuleError("foreground_decision_invalid")
        asset = self._asset(customer_id, asset_id)
        row = self._version(customer_id, asset.id, version_id)
        if row.manifest_sha256 != manifest_sha256:
            raise DomainRuleError("foreground_manifest_mismatch")
        if row.state != "proposed":
            if (
                row.state == decision
                and row.confirmation_key == confirmation_key
                and row.manifest_sha256 == manifest_sha256
            ):
                return self._outcome(row, created=False)
            if row.state == "rejected":
                raise DomainRuleError("foreground_version_rejected")
            raise DomainRuleError("foreground_version_immutable")

        request, result = self._reconstruct(customer_id, asset, row)
        validate_extracted_foreground(result)
        recovered_manifest = foreground_manifest_evidence_sha256(
            request,
            extractor_version=row.extractor_version,
            foreground_sha256=row.foreground_sha256,
            mask_sha256=row.mask_sha256,
            width=row.width,
            height=row.height,
            pipeline_manifest_sha256=row.pipeline_manifest_sha256,
            stage_evidence_sha256=row.stage_receipts_sha256,
        )
        if recovered_manifest != row.manifest_sha256:
            raise DomainRuleError("foreground_manifest_mismatch")
        if decision == "confirmed" and not self._has_confirmed_identity(
            customer_id, asset, row
        ):
            raise DomainRuleError("foreground_identity_card_required")
        shadow_decision = self._shadow_decision(customer_id, row.id)
        if (
            decision == "confirmed"
            and shadow_decision is not None
            and shadow_decision.verdict == "blocked"
        ):
            raise DomainRuleError("foreground_shadow_blocked")

        row.state = decision
        row.confirmation_key = confirmation_key
        row.decided_at = datetime.now(UTC)
        row.updated_at = row.decided_at
        self._session.commit()
        return self._outcome(row, created=True)

    def edit(
        self,
        customer_id: UUID,
        asset_id: UUID,
        *,
        base_version_id: UUID,
        base_manifest_sha256: str,
        mask_png: bytes,
        editor_version: str,
        edit_manifest_sha256: str,
        idempotency_key: str,
        operation_counts: MaskEditOperationCounts,
    ) -> ProductForegroundOutcome:
        """Append one server-rebuilt foreground proposal and immutable edit receipt."""
        existing = self._session.scalar(
            select(ProductForegroundMaskRevision).where(
                ProductForegroundMaskRevision.customer_id == customer_id,
                ProductForegroundMaskRevision.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.edit_manifest_sha256 != edit_manifest_sha256:
                raise DomainRuleError("foreground_edit_conflict")
            row = self._version(
                customer_id,
                existing.product_asset_id,
                existing.result_foreground_version_id,
            )
            return self._outcome(row, created=False)

        asset = self._asset(customer_id, asset_id)
        base = self._version(customer_id, asset.id, base_version_id)
        if (
            base.manifest_sha256 != base_manifest_sha256
            or base.source_content_sha256 != asset.content_sha256
        ):
            raise DomainRuleError("foreground_edit_stale")
        try:
            source = self._content_store.read_original(
                customer_id, asset.id, asset.content_sha256
            )
            base_mask = self._content_store.read_product_mask(
                customer_id, base.id, base.mask_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("foreground_edit_stale") from error

        normalized_mask = normalize_edited_mask(mask_png, asset.width, asset.height)
        metrics = compute_mask_edit_metrics(
            base_mask, normalized_mask, asset.width, asset.height
        )
        rebuilt: EditedForeground = rebuild_foreground_from_original(
            source, normalized_mask, asset.width, asset.height
        )
        authoritative_manifest = mask_edit_manifest_sha256(
            customer_id=str(customer_id),
            asset_id=str(asset.id),
            base_version_id=str(base.id),
            source_content_sha256=asset.content_sha256,
            base_manifest_sha256=base.manifest_sha256,
            base_mask_sha256=base.mask_sha256,
            result_mask_pixels_sha256=mask_pixels_sha256(
                rebuilt.mask_png, rebuilt.width, rebuilt.height
            ),
            editor_version=editor_version,
            magic_wand_count=operation_counts.magic_wand_count,
            erase_stroke_count=operation_counts.erase_stroke_count,
            restore_stroke_count=operation_counts.restore_stroke_count,
        )
        if authoritative_manifest != edit_manifest_sha256:
            raise DomainRuleError("foreground_edit_manifest_mismatch")

        request = ForegroundExtractionRequest(
            customer_id=customer_id,
            asset_id=asset.id,
            product_id=asset.product_id,
            source_content=source,
            source_content_sha256=asset.content_sha256,
            mime_type=asset.mime_type,
            width=asset.width,
            height=asset.height,
        )
        extracted = ExtractedForeground(
            foreground_png=rebuilt.foreground_png,
            foreground_sha256=rebuilt.foreground_sha256,
            mask_png=rebuilt.mask_png,
            mask_sha256=rebuilt.mask_sha256,
            width=rebuilt.width,
            height=rebuilt.height,
            extractor_version=editor_version,
        )
        result_manifest = foreground_manifest_sha256(request, extracted)
        next_version = (
            self._session.scalar(
                select(func.max(ProductForegroundVersion.version)).where(
                    ProductForegroundVersion.customer_id == customer_id,
                    ProductForegroundVersion.product_asset_id == asset.id,
                )
            )
            or 0
        ) + 1
        result_id = uuid4()
        foreground_persisted = False
        mask_persisted = False
        try:
            self._content_store.persist_product_foreground(
                customer_id,
                result_id,
                rebuilt.foreground_png,
                rebuilt.foreground_sha256,
            )
            foreground_persisted = True
            self._content_store.persist_product_mask(
                customer_id, result_id, rebuilt.mask_png, rebuilt.mask_sha256
            )
            mask_persisted = True
            quality_flags = [
                "human_mask_edit",
                *(("large_mask_change",) if metrics.large_change_warning else ()),
            ]
            row = ProductForegroundVersion(
                id=result_id,
                customer_id=customer_id,
                product_asset_id=asset.id,
                product_id=asset.product_id,
                version=next_version,
                source_content_sha256=asset.content_sha256,
                foreground_sha256=rebuilt.foreground_sha256,
                mask_sha256=rebuilt.mask_sha256,
                width=asset.width,
                height=asset.height,
                extractor_version=editor_version,
                manifest_sha256=result_manifest,
                state="proposed",
                quality_flags=quality_flags,
            )
            self._session.add(row)
            self._session.flush()
            revision = ProductForegroundMaskRevision(
                id=uuid4(),
                customer_id=customer_id,
                product_asset_id=asset.id,
                base_foreground_version_id=base.id,
                result_foreground_version_id=result_id,
                source_content_sha256=asset.content_sha256,
                base_mask_sha256=base.mask_sha256,
                result_mask_sha256=rebuilt.mask_sha256,
                edit_manifest_sha256=authoritative_manifest,
                editor_version=editor_version,
                magic_wand_count=operation_counts.magic_wand_count,
                erase_stroke_count=operation_counts.erase_stroke_count,
                restore_stroke_count=operation_counts.restore_stroke_count,
                added_pixel_count=metrics.added_pixel_count,
                deleted_pixel_count=metrics.deleted_pixel_count,
                result_pixel_count=metrics.result_pixel_count,
                component_count=metrics.component_count,
                idempotency_key=idempotency_key,
            )
            self._session.add(revision)
            self._session.commit()
        except Exception:
            self._session.rollback()
            if mask_persisted:
                self._discard_mask(customer_id, result_id, rebuilt.mask_sha256)
            if foreground_persisted:
                self._discard_foreground(
                    customer_id, result_id, rebuilt.foreground_sha256
                )
            raise
        return self._outcome(row, created=True)

    def versions(
        self, customer_id: UUID, asset_id: UUID
    ) -> tuple[ProductForegroundOutcome, ...]:
        asset = self._asset(customer_id, asset_id)
        rows = self._session.scalars(
            select(ProductForegroundVersion)
            .where(
                ProductForegroundVersion.customer_id == customer_id,
                ProductForegroundVersion.product_asset_id == asset.id,
            )
            .order_by(ProductForegroundVersion.version.desc())
        ).all()
        return tuple(self._outcome(row, created=False) for row in rows)

    def read_foreground(self, customer_id: UUID, version_id: UUID) -> bytes:
        row = self._version_by_id(customer_id, version_id)
        try:
            return self._content_store.read_product_foreground(
                customer_id, row.id, row.foreground_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("foreground_content_unavailable") from error

    def read_mask(self, customer_id: UUID, version_id: UUID) -> bytes:
        row = self._version_by_id(customer_id, version_id)
        try:
            return self._content_store.read_product_mask(
                customer_id, row.id, row.mask_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("foreground_content_unavailable") from error

    def ensure_disagreement_evidence(
        self, customer_id: UUID, version_id: UUID
    ) -> ProductForegroundOutcome:
        """Backfill one sealed coarse mask only when a rerun reproduces the final mask."""
        row = self._version_by_id(customer_id, version_id)
        existing = self._disagreement_evidence(customer_id, version_id)
        if existing is not None:
            return self._outcome(row, created=False)
        if self._extractor is None or row.pipeline_manifest_sha256 is None:
            raise DomainRuleError("foreground_disagreement_unavailable")
        asset = self._asset(customer_id, row.product_asset_id)
        try:
            source = self._content_store.read_original(
                customer_id, asset.id, row.source_content_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("foreground_disagreement_unavailable") from error
        request = ForegroundExtractionRequest(
            customer_id=customer_id,
            asset_id=asset.id,
            product_id=asset.product_id,
            source_content=source,
            source_content_sha256=row.source_content_sha256,
            mime_type=asset.mime_type,
            width=asset.width,
            height=asset.height,
        )
        result = self._extractor.extract(request)
        validate_extracted_foreground(result)
        if (
            result.coarse_mask_png is None
            or result.coarse_mask_sha256 is None
            or result.mask_sha256 != row.mask_sha256
            or result.pipeline_manifest_sha256 != row.pipeline_manifest_sha256
            or stage_receipts_sha256(result.stage_receipts) != row.stage_receipts_sha256
        ):
            raise DomainRuleError("foreground_disagreement_reproduction_mismatch")
        storage_id = self._coarse_mask_storage_id(row.id)
        persisted = False
        try:
            self._content_store.persist_product_mask(
                customer_id,
                storage_id,
                result.coarse_mask_png,
                result.coarse_mask_sha256,
            )
            persisted = True
            self._session.add(
                ProductForegroundDisagreementEvidence(
                    customer_id=customer_id,
                    foreground_version_id=row.id,
                    coarse_mask_sha256=result.coarse_mask_sha256,
                )
            )
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            if self._disagreement_evidence(customer_id, version_id) is None:
                if persisted:
                    self._discard_mask(
                        customer_id,
                        storage_id,
                        result.coarse_mask_sha256,
                    )
                raise
        except Exception:
            self._session.rollback()
            if persisted:
                self._discard_mask(customer_id, storage_id, result.coarse_mask_sha256)
            raise
        return self._outcome(row, created=True)

    def read_disagreement_map(self, customer_id: UUID, version_id: UUID) -> bytes:
        row = self._version_by_id(customer_id, version_id)
        evidence = self._disagreement_evidence(customer_id, version_id)
        if evidence is None:
            raise DomainRuleError("foreground_disagreement_unavailable")
        try:
            source = self._content_store.read_original(
                customer_id, row.product_asset_id, row.source_content_sha256
            )
            refined = self._content_store.read_product_mask(
                customer_id, row.id, row.mask_sha256
            )
            coarse = self._content_store.read_product_mask(
                customer_id,
                self._coarse_mask_storage_id(row.id),
                evidence.coarse_mask_sha256,
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("foreground_disagreement_unavailable") from error
        return self._render_disagreement_map(
            source, coarse, refined, row.width, row.height
        )

    def _asset(self, customer_id: UUID, asset_id: UUID) -> ProductAsset:
        asset = self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == customer_id,
                ProductAsset.id == asset_id,
                ProductAsset.confirmed.is_(True),
                ProductAsset.identity == "original_real_photo",
            )
        )
        if asset is None:
            raise DomainRuleError("foreground_asset_unavailable")
        return asset

    def _version(
        self, customer_id: UUID, asset_id: UUID, version_id: UUID
    ) -> ProductForegroundVersion:
        row = self._session.scalar(
            select(ProductForegroundVersion).where(
                ProductForegroundVersion.customer_id == customer_id,
                ProductForegroundVersion.product_asset_id == asset_id,
                ProductForegroundVersion.id == version_id,
            )
        )
        if row is None:
            raise DomainRuleError("foreground_asset_unavailable")
        return row

    def _version_by_id(
        self, customer_id: UUID, version_id: UUID
    ) -> ProductForegroundVersion:
        row = self._session.scalar(
            select(ProductForegroundVersion).where(
                ProductForegroundVersion.customer_id == customer_id,
                ProductForegroundVersion.id == version_id,
            )
        )
        if row is None:
            raise DomainRuleError("foreground_asset_unavailable")
        return row

    def _reconstruct(
        self,
        customer_id: UUID,
        asset: ProductAsset,
        row: ProductForegroundVersion,
    ) -> tuple[ForegroundExtractionRequest, ExtractedForeground]:
        try:
            source = self._content_store.read_original(
                customer_id, asset.id, row.source_content_sha256
            )
            foreground = self._content_store.read_product_foreground(
                customer_id, row.id, row.foreground_sha256
            )
            mask = self._content_store.read_product_mask(
                customer_id, row.id, row.mask_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("foreground_manifest_mismatch") from error
        if asset.content_sha256 != row.source_content_sha256:
            raise DomainRuleError("foreground_manifest_mismatch")
        return (
            ForegroundExtractionRequest(
                customer_id=customer_id,
                asset_id=asset.id,
                product_id=asset.product_id,
                source_content=source,
                source_content_sha256=row.source_content_sha256,
                mime_type=asset.mime_type,
                width=asset.width,
                height=asset.height,
            ),
            ExtractedForeground(
                foreground_png=foreground,
                foreground_sha256=row.foreground_sha256,
                mask_png=mask,
                mask_sha256=row.mask_sha256,
                width=row.width,
                height=row.height,
                extractor_version="persisted-foreground-recovery-v1",
            ),
        )

    def _has_confirmed_identity(
        self,
        customer_id: UUID,
        asset: ProductAsset,
        row: ProductForegroundVersion,
    ) -> bool:
        return (
            self._session.scalar(
                select(ProductIdentityCardVersion.id).where(
                    ProductIdentityCardVersion.customer_id == customer_id,
                    ProductIdentityCardVersion.product_asset_id == asset.id,
                    ProductIdentityCardVersion.product_id == asset.product_id,
                    ProductIdentityCardVersion.source_content_sha256
                    == row.source_content_sha256,
                )
            )
            is not None
        )

    def _outcome(
        self,
        row: ProductForegroundVersion,
        *,
        quality: ForegroundQuality | None = None,
        created: bool,
    ) -> ProductForegroundOutcome:
        quality_flags = list(row.quality_flags)
        if (
            quality is not None
            and quality.touches_edge
            and "touches_edge" not in quality_flags
        ):
            quality_flags.append("touches_edge")
        shadow = self._shadow_decision(row.customer_id, row.id)
        disagreement = self._disagreement_profile(row)
        return ProductForegroundOutcome(
            id=row.id,
            asset_id=row.product_asset_id,
            product_id=row.product_id,
            version=row.version,
            source_content_sha256=row.source_content_sha256,
            foreground_sha256=row.foreground_sha256,
            mask_sha256=row.mask_sha256,
            width=row.width,
            height=row.height,
            extractor_version=row.extractor_version,
            manifest_sha256=row.manifest_sha256,
            state=cast(Literal["proposed", "confirmed", "rejected"], row.state),
            quality_flags=tuple(quality_flags),
            shadow_verdict=(
                cast(RiskVerdict, shadow.verdict) if shadow is not None else None
            ),
            shadow_reason_codes=(
                tuple(shadow.reason_codes) if shadow is not None else ()
            ),
            shadow_model_manifest_sha256=(
                shadow.model_manifest_sha256 if shadow is not None else None
            ),
            pipeline_manifest_sha256=row.pipeline_manifest_sha256,
            stage_receipts_sha256=row.stage_receipts_sha256,
            disagreement_available=(
                self._disagreement_evidence(row.customer_id, row.id) is not None
            ),
            disagreement_classification=(
                disagreement.classification if disagreement is not None else None
            ),
            disagreement_ratio=(
                disagreement.disagreement_ratio if disagreement is not None else None
            ),
            coarse_only_ratio=(
                disagreement.coarse_only_ratio if disagreement is not None else None
            ),
            refined_only_ratio=(
                disagreement.refined_only_ratio if disagreement is not None else None
            ),
            coarse_only_detached_ratio=(
                disagreement.coarse_only_detached_ratio
                if disagreement is not None
                else None
            ),
            created=created,
        )

    def _disagreement_profile(
        self, row: ProductForegroundVersion
    ) -> StageDisagreementProfile | None:
        evidence = self._disagreement_evidence(row.customer_id, row.id)
        if evidence is None:
            return None
        try:
            refined = self._content_store.read_product_mask(
                row.customer_id, row.id, row.mask_sha256
            )
            coarse = self._content_store.read_product_mask(
                row.customer_id,
                self._coarse_mask_storage_id(row.id),
                evidence.coarse_mask_sha256,
            )
            return profile_extraction_stage_disagreement(
                coarse, refined, row.width, row.height
            )
        except (AssetContentStoreError, DomainRuleError):
            return None

    def _disagreement_evidence(
        self, customer_id: UUID, foreground_version_id: UUID
    ) -> ProductForegroundDisagreementEvidence | None:
        return self._session.scalar(
            select(ProductForegroundDisagreementEvidence).where(
                ProductForegroundDisagreementEvidence.customer_id == customer_id,
                ProductForegroundDisagreementEvidence.foreground_version_id
                == foreground_version_id,
            )
        )

    @staticmethod
    def _coarse_mask_storage_id(version_id: UUID) -> UUID:
        return uuid5(version_id, "sealed-coarse-mask-v1")

    @staticmethod
    def _render_disagreement_map(
        source: bytes,
        coarse_mask: bytes,
        refined_mask: bytes,
        width: int,
        height: int,
    ) -> bytes:
        try:
            with Image.open(BytesIO(source)) as source_image:
                source_image.load()
                base = source_image.convert("RGBA")
            with Image.open(BytesIO(coarse_mask)) as coarse_image:
                coarse_image.load()
                coarse = coarse_image.convert("L")
            with Image.open(BytesIO(refined_mask)) as refined_image:
                refined_image.load()
                refined = refined_image.convert("L")
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError):
            raise DomainRuleError("foreground_disagreement_unavailable") from None
        if (
            base.size != (width, height)
            or coarse.size != base.size
            or refined.size != base.size
        ):
            raise DomainRuleError("foreground_disagreement_unavailable")
        coarse_binary = coarse.point(lambda value: 255 if value >= 128 else 0)
        refined_binary = refined.point(lambda value: 255 if value >= 128 else 0)
        missing = ImageChops.subtract(coarse_binary, refined_binary)
        extra = ImageChops.subtract(refined_binary, coarse_binary)
        dimmed = ImageEnhance.Brightness(base).enhance(0.45)
        red = Image.new("RGBA", base.size, (255, 32, 64, 255))
        yellow = Image.new("RGBA", base.size, (255, 196, 0, 255))
        rendered = Image.composite(red, dimmed, missing)
        rendered = Image.composite(yellow, rendered, extra)
        output = BytesIO()
        rendered.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def _evaluate_shadow(
        self,
        request: ForegroundExtractionRequest,
        baseline: ExtractedForeground,
        quality: ForegroundQuality,
    ) -> tuple[ShadowRiskAssessment, str, bool] | None:
        evaluator = self._shadow_evaluator
        if evaluator is None:
            return None
        evaluator_digest = evaluator.model_manifest_sha256
        if len(evaluator_digest) != 64:
            raise DomainRuleError("foreground_shadow_configuration_invalid")
        model_digest = evaluator_digest
        try:
            signals = evaluator.evaluate(request, baseline, quality)
            if baseline.pipeline_manifest_sha256 is not None:
                evidence_complete = (
                    signals.evidence_complete
                    and baseline.coarse_mask_png is not None
                    and len(baseline.stage_receipts) == 2
                )
                if baseline.coarse_mask_png is None:
                    raise DomainRuleError("foreground_pipeline_evidence_invalid")
                signals = compare_extraction_stages(
                    baseline.coarse_mask_png,
                    baseline.mask_png,
                    baseline.width,
                    baseline.height,
                    high_risk_components=signals.high_risk_components,
                    evidence_complete=evidence_complete,
                )
                model_digest = self._shadow_identity_sha256(
                    request,
                    baseline,
                    evaluator_digest=evaluator_digest,
                    config_sha256=evaluator.thresholds.config_sha256,
                )
            assessment = assess_shadow_risk(signals, evaluator.thresholds)
            if (
                baseline.pipeline_manifest_sha256 is None
                and assessment.verdict == "auto_eligible"
            ):
                assessment = ShadowRiskAssessment(
                    verdict="human_review_required",
                    reason_codes=("legacy_pipeline_evidence",),
                    config_sha256=assessment.config_sha256,
                )
            return assessment, model_digest, signals.evidence_complete
        except DomainRuleError:
            raise
        except Exception:  # noqa: BLE001 - do not leak candidate paths or private bytes
            return (
                ShadowRiskAssessment(
                    verdict="blocked",
                    reason_codes=("shadow_evaluator_failed",),
                    config_sha256=evaluator.thresholds.config_sha256,
                ),
                model_digest,
                False,
            )

    @staticmethod
    def _shadow_identity_sha256(
        request: ForegroundExtractionRequest,
        result: ExtractedForeground,
        *,
        evaluator_digest: str,
        config_sha256: str,
    ) -> str:
        payload = {
            "config_sha256": config_sha256,
            "evaluator_manifest_sha256": evaluator_digest,
            "pipeline_manifest_sha256": result.pipeline_manifest_sha256,
            "source_content_sha256": request.source_content_sha256,
            "stage_receipts": [
                {
                    "artifact_sha256": receipt.artifact_sha256,
                    "output_sha256": receipt.output_sha256,
                    "parameters_sha256": receipt.parameters_sha256,
                    "role": receipt.role,
                }
                for receipt in result.stage_receipts
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return sha256(encoded).hexdigest()

    def _shadow_decision(
        self, customer_id: UUID, foreground_version_id: UUID
    ) -> ForegroundShadowDecision | None:
        return self._session.scalar(
            select(ForegroundShadowDecision).where(
                ForegroundShadowDecision.customer_id == customer_id,
                ForegroundShadowDecision.foreground_version_id == foreground_version_id,
            )
        )

    def _discard_foreground(
        self, customer_id: UUID, version_id: UUID, digest: str
    ) -> None:
        try:
            self._content_store.discard_product_foreground(
                customer_id, version_id, digest
            )
        except AssetContentStoreError:
            pass

    def _discard_mask(self, customer_id: UUID, version_id: UUID, digest: str) -> None:
        try:
            self._content_store.discard_product_mask(customer_id, version_id, digest)
        except AssetContentStoreError:
            pass
