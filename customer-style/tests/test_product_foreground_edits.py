from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import RiskSignals
from opc_style.domain.product_foreground import (
    ExtractedForeground,
    ForegroundExtractionRequest,
    ForegroundStageReceipt,
    foreground_manifest_sha256,
    mask_edit_manifest_sha256,
    mask_pixels_sha256,
    normalize_edited_mask,
    rebuild_foreground_from_original,
)
from opc_style.models import (
    Customer,
    ForegroundShadowDecision,
    ProductAsset,
    ProductForegroundMaskRevision,
    ProductForegroundVersion,
)
from opc_style.services.foreground_risk_gate import (
    LocalStageDisagreementEvaluator,
    RiskThresholds,
)
from opc_style.services.product_foregrounds import (
    MaskEditOperationCounts,
    ProductForegroundService,
)
from opc_style.services.real_photos import AssetContentStoreError

CUSTOMER_A = UUID("00000000-0000-0000-0000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-0000-0000-0000000000b2")


class MemoryContentStore:
    def __init__(self) -> None:
        self.originals: dict[tuple[UUID, UUID], bytes] = {}
        self.foregrounds: dict[tuple[UUID, UUID], bytes] = {}
        self.masks: dict[tuple[UUID, UUID], bytes] = {}

    def read_original(self, customer_id, asset_id, expected_sha256):
        content = self.originals[(customer_id, asset_id)]
        if sha256(content).hexdigest() != expected_sha256:
            raise AssetContentStoreError("digest")
        return content

    def persist_product_foreground(
        self, customer_id, version_id, content, expected_sha256
    ):
        assert sha256(content).hexdigest() == expected_sha256
        self.foregrounds[(customer_id, version_id)] = content

    def persist_product_mask(self, customer_id, version_id, content, expected_sha256):
        assert sha256(content).hexdigest() == expected_sha256
        self.masks[(customer_id, version_id)] = content

    def read_product_foreground(self, customer_id, version_id, expected_sha256):
        content = self.foregrounds[(customer_id, version_id)]
        if sha256(content).hexdigest() != expected_sha256:
            raise AssetContentStoreError("digest")
        return content

    def read_product_mask(self, customer_id, version_id, expected_sha256):
        content = self.masks[(customer_id, version_id)]
        if sha256(content).hexdigest() != expected_sha256:
            raise AssetContentStoreError("digest")
        return content

    def discard_product_foreground(self, customer_id, version_id, expected_sha256):
        content = self.foregrounds.get((customer_id, version_id))
        if content is not None and sha256(content).hexdigest() == expected_sha256:
            self.foregrounds.pop((customer_id, version_id))

    def discard_product_mask(self, customer_id, version_id, expected_sha256):
        content = self.masks.get((customer_id, version_id))
        if content is not None and sha256(content).hexdigest() == expected_sha256:
            self.masks.pop((customer_id, version_id))


class PreviewExtractor:
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        mask = normalize_edited_mask(_mask(), request.width, request.height)
        rebuilt = rebuild_foreground_from_original(
            request.source_content, mask, request.width, request.height
        )
        return ExtractedForeground(
            foreground_png=rebuilt.foreground_png,
            foreground_sha256=rebuilt.foreground_sha256,
            mask_png=rebuilt.mask_png,
            mask_sha256=rebuilt.mask_sha256,
            width=request.width,
            height=request.height,
            extractor_version="preview-baseline-v1",
        )


class PipelinePreviewExtractor(PreviewExtractor):
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        result = super().extract(request)
        return ExtractedForeground(
            foreground_png=result.foreground_png,
            foreground_sha256=result.foreground_sha256,
            mask_png=result.mask_png,
            mask_sha256=result.mask_sha256,
            width=result.width,
            height=result.height,
            extractor_version="local-sam2-birefnet-v1:123456789abc",
            coarse_mask_png=result.mask_png,
            coarse_mask_sha256=result.mask_sha256,
            stage_receipts=(
                ForegroundStageReceipt(
                    role="coarse_segmenter",
                    artifact_sha256="a" * 64,
                    output_sha256=result.mask_sha256,
                    parameters_sha256="b" * 64,
                ),
                ForegroundStageReceipt(
                    role="alpha_refiner",
                    artifact_sha256="c" * 64,
                    output_sha256=result.mask_sha256,
                    parameters_sha256="d" * 64,
                ),
            ),
            pipeline_manifest_sha256="e" * 64,
        )


class DisagreeingPipelinePreviewExtractor(PipelinePreviewExtractor):
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        result = super().extract(request)
        coarse = Image.new("L", (request.width, request.height), 0)
        for y in range(1, 9):
            for x in range(1, 9):
                coarse.putpixel((x, y), 255)
        output = BytesIO()
        coarse.save(output, format="PNG")
        coarse_bytes = output.getvalue()
        coarse_sha256 = sha256(coarse_bytes).hexdigest()
        return replace(
            result,
            coarse_mask_png=coarse_bytes,
            coarse_mask_sha256=coarse_sha256,
            stage_receipts=(
                replace(result.stage_receipts[0], output_sha256=coarse_sha256),
                result.stage_receipts[1],
            ),
        )


class FakeShadowEvaluator:
    model_manifest_sha256 = "9" * 64
    thresholds = RiskThresholds()

    def __init__(self, signals: RiskSignals | None = None, *, fails: bool = False) -> None:
        self._signals = signals
        self._fails = fails

    def evaluate(self, request, baseline, quality):
        del request, baseline, quality
        if self._fails:
            raise RuntimeError("/private/customer/image.png")
        assert self._signals is not None
        return self._signals


def _source() -> bytes:
    image = Image.new("RGB", (10, 10))
    for y in range(10):
        for x in range(10):
            image.putpixel((x, y), (x * 20, y * 20, (x + y) * 10))
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _mask(*, variant: int = 0) -> bytes:
    image = Image.new("L", (10, 10), 0)
    for y in range(2, 8):
        for x in range(2, 8):
            image.putpixel((x, y), 255)
    image.putpixel((2 + variant, 2), 0)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _seed(session: Session, store: MemoryContentStore, *, state: str = "proposed"):
    source = _source()
    session.add_all(
        [
            Customer(id=CUSTOMER_A, display_name="A"),
            Customer(id=CUSTOMER_B, display_name="B"),
        ]
    )
    asset = ProductAsset(
        id=uuid4(),
        customer_id=CUSTOMER_A,
        product_id="bracelet-1",
        photo_id=f"photo-{uuid4()}",
        identity="original_real_photo",
        confirmed=True,
        source_type="upload",
        quality_flags=[],
        occurred_at=datetime.now(UTC),
        content_sha256=sha256(source).hexdigest(),
        mime_type="image/png",
        width=10,
        height=10,
    )
    session.add(asset)
    session.flush()
    base_mask = normalize_edited_mask(_mask(variant=0), 10, 10)
    rebuilt = rebuild_foreground_from_original(source, base_mask, 10, 10)
    extracted = ExtractedForeground(
        foreground_png=rebuilt.foreground_png,
        foreground_sha256=rebuilt.foreground_sha256,
        mask_png=rebuilt.mask_png,
        mask_sha256=rebuilt.mask_sha256,
        width=10,
        height=10,
        extractor_version="fake-v1",
    )
    request = ForegroundExtractionRequest(
        customer_id=CUSTOMER_A,
        asset_id=asset.id,
        product_id=asset.product_id,
        source_content=source,
        source_content_sha256=asset.content_sha256,
        mime_type=asset.mime_type,
        width=10,
        height=10,
    )
    base = ProductForegroundVersion(
        id=uuid4(),
        customer_id=CUSTOMER_A,
        product_asset_id=asset.id,
        product_id=asset.product_id,
        version=1,
        source_content_sha256=asset.content_sha256,
        foreground_sha256=extracted.foreground_sha256,
        mask_sha256=extracted.mask_sha256,
        width=10,
        height=10,
        extractor_version="fake-v1",
        manifest_sha256=foreground_manifest_sha256(request, extracted),
        state=state,
        quality_flags=[],
        confirmation_key="prior-decision" if state != "proposed" else None,
        decided_at=datetime.now(UTC) if state != "proposed" else None,
    )
    session.add(base)
    session.commit()
    store.originals[(CUSTOMER_A, asset.id)] = source
    store.foregrounds[(CUSTOMER_A, base.id)] = rebuilt.foreground_png
    store.masks[(CUSTOMER_A, base.id)] = rebuilt.mask_png
    return asset, base


def _edit(
    service: ProductForegroundService,
    asset: ProductAsset,
    base: ProductForegroundVersion,
    *,
    mask: bytes | None = None,
    key: str = "edit-1",
    manifest: str | None = None,
    base_manifest: str | None = None,
):
    normalized = normalize_edited_mask(mask or _mask(variant=1), 10, 10)
    edit_manifest = mask_edit_manifest_sha256(
        customer_id=str(CUSTOMER_A),
        asset_id=str(asset.id),
        base_version_id=str(base.id),
        source_content_sha256=asset.content_sha256,
        base_manifest_sha256=base_manifest or base.manifest_sha256,
        base_mask_sha256=base.mask_sha256,
        result_mask_pixels_sha256=mask_pixels_sha256(normalized, 10, 10),
        editor_version="human-mask-editor-v1",
        magic_wand_count=1,
        erase_stroke_count=0,
        restore_stroke_count=0,
    )
    return service.edit(
        CUSTOMER_A,
        asset.id,
        base_version_id=base.id,
        base_manifest_sha256=base_manifest or base.manifest_sha256,
        mask_png=normalized,
        editor_version="human-mask-editor-v1",
        edit_manifest_sha256=manifest or edit_manifest,
        idempotency_key=key,
        operation_counts=MaskEditOperationCounts(1, 0, 0),
    )


def test_shadow_verdict_is_persisted_but_never_auto_confirms_preview(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, _ = _seed(session, store)
    signals = RiskSignals(
        coarse_refined_iou=0.70,
        area_delta_ratio=0.20,
        touches_edge=False,
        component_count=2,
        fine_structure_ratio=0.98,
        high_risk_components=("pendant",),
        evidence_complete=True,
    )
    service = ProductForegroundService(
        session,
        content_store=store,
        extractor=PreviewExtractor(),
        shadow_evaluator=FakeShadowEvaluator(signals),
    )

    outcome = service.preview(CUSTOMER_A, asset.id)

    assert outcome.state == "proposed"
    assert outcome.shadow_verdict == "human_review_required"
    assert "model_disagreement" in outcome.shadow_reason_codes
    decision = session.scalar(
        select(ForegroundShadowDecision).where(
            ForegroundShadowDecision.foreground_version_id == outcome.id
        )
    )
    assert decision is not None
    assert decision.model_manifest_sha256 == "9" * 64
    assert decision.evidence_complete is True


def test_pipeline_shadow_identity_binds_source_stages_pipeline_and_config(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, _ = _seed(session, store)
    signals = RiskSignals(
        coarse_refined_iou=0.20,
        area_delta_ratio=0.80,
        touches_edge=True,
        component_count=99,
        fine_structure_ratio=0.10,
        high_risk_components=("pendant",),
        evidence_complete=True,
    )
    service = ProductForegroundService(
        session,
        content_store=store,
        extractor=PipelinePreviewExtractor(),
        shadow_evaluator=FakeShadowEvaluator(signals),
    )

    outcome = service.preview(CUSTOMER_A, asset.id)

    decision = session.scalar(
        select(ForegroundShadowDecision).where(
            ForegroundShadowDecision.foreground_version_id == outcome.id
        )
    )
    assert decision is not None
    assert decision.model_manifest_sha256 != FakeShadowEvaluator.model_manifest_sha256
    assert len(decision.model_manifest_sha256) == 64
    assert decision.evidence_complete is True
    assert decision.pipeline_manifest_sha256 == "e" * 64
    assert decision.stage_receipts_sha256 is not None
    assert decision.risk_parameters_sha256 == RiskThresholds().config_sha256
    assert outcome.shadow_verdict == "human_review_required"
    assert outcome.shadow_reason_codes == ("high_risk_component:pendant",)


def test_local_pipeline_stage_disagreement_is_exposed_for_human_review(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, _ = _seed(session, store)

    outcome = ProductForegroundService(
        session,
        content_store=store,
        extractor=DisagreeingPipelinePreviewExtractor(),
        shadow_evaluator=LocalStageDisagreementEvaluator(),
    ).preview(CUSTOMER_A, asset.id)

    assert outcome.state == "proposed"
    assert outcome.shadow_verdict == "human_review_required"
    assert "model_disagreement" in outcome.shadow_reason_codes
    assert "abnormal_area_delta" in outcome.shadow_reason_codes


def test_changed_source_pipeline_or_parameters_invalidates_shadow_decision(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, _ = _seed(session, store)
    source = store.originals[(CUSTOMER_A, asset.id)]
    request = ForegroundExtractionRequest(
        customer_id=CUSTOMER_A,
        asset_id=asset.id,
        product_id=asset.product_id,
        source_content=source,
        source_content_sha256=asset.content_sha256,
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
    )
    result = PipelinePreviewExtractor().extract(request)
    original = ProductForegroundService._shadow_identity_sha256(
        request,
        result,
        evaluator_digest="9" * 64,
        config_sha256="f" * 64,
    )
    changed_pipeline = ProductForegroundService._shadow_identity_sha256(
        request,
        replace(result, pipeline_manifest_sha256="0" * 64),
        evaluator_digest="9" * 64,
        config_sha256="f" * 64,
    )
    changed_parameters = ProductForegroundService._shadow_identity_sha256(
        request,
        replace(
            result,
            stage_receipts=(
                replace(result.stage_receipts[0], parameters_sha256="0" * 64),
                result.stage_receipts[1],
            ),
        ),
        evaluator_digest="9" * 64,
        config_sha256="f" * 64,
    )
    changed_source = ProductForegroundService._shadow_identity_sha256(
        replace(request, source_content_sha256="0" * 64),
        result,
        evaluator_digest="9" * 64,
        config_sha256="f" * 64,
    )

    assert len({original, changed_pipeline, changed_parameters, changed_source}) == 4


def test_shadow_worker_failure_is_sanitized_and_blocks_preview(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, _ = _seed(session, store)
    service = ProductForegroundService(
        session,
        content_store=store,
        extractor=PreviewExtractor(),
        shadow_evaluator=FakeShadowEvaluator(fails=True),
    )

    outcome = service.preview(CUSTOMER_A, asset.id)

    assert outcome.state == "proposed"
    assert outcome.shadow_verdict == "blocked"
    assert outcome.shadow_reason_codes == ("shadow_evaluator_failed",)


def test_legacy_foreground_is_recoverable_but_not_auto_eligible(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, _ = _seed(session, store)
    low_risk = RiskSignals(
        coarse_refined_iou=1.0,
        area_delta_ratio=0.0,
        touches_edge=False,
        component_count=1,
        fine_structure_ratio=1.0,
        high_risk_components=(),
        evidence_complete=True,
    )
    outcome = ProductForegroundService(
        session,
        content_store=store,
        extractor=PreviewExtractor(),
        shadow_evaluator=FakeShadowEvaluator(low_risk),
    ).preview(CUSTOMER_A, asset.id)

    assert outcome.shadow_verdict == "human_review_required"
    assert outcome.shadow_reason_codes == ("legacy_pipeline_evidence",)


def test_edit_rebuilds_foreground_from_original_and_appends_proposed_version(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, base = _seed(session, store)

    outcome = _edit(ProductForegroundService(session, content_store=store, extractor=None), asset, base)

    assert outcome.version == 2
    assert outcome.state == "proposed"
    assert outcome.extractor_version == "human-mask-editor-v1"
    assert base.state == "proposed"
    revision = session.scalar(
        select(ProductForegroundMaskRevision).where(
            ProductForegroundMaskRevision.result_foreground_version_id == outcome.id
        )
    )
    assert revision is not None
    assert revision.base_foreground_version_id == base.id
    assert revision.idempotency_key == "edit-1"
    with (
        Image.open(BytesIO(store.foregrounds[(CUSTOMER_A, outcome.id)])) as result,
        Image.open(BytesIO(store.originals[(CUSTOMER_A, asset.id)])) as original,
    ):
        assert result.getpixel((5, 5))[:3] == original.getpixel((5, 5))


@pytest.mark.parametrize("state", ["proposed", "confirmed", "rejected"])
def test_edit_copies_proposed_confirmed_or_rejected_base_into_a_fresh_proposal(
    session: Session, state: str
) -> None:
    store = MemoryContentStore()
    asset, base = _seed(session, store, state=state)
    outcome = _edit(ProductForegroundService(session, content_store=store, extractor=None), asset, base)
    assert outcome.state == "proposed"
    assert session.get(ProductForegroundVersion, base.id).state == state


def test_edit_rejects_cross_customer_cross_asset_stale_and_tampered_base(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, base = _seed(session, store)
    service = ProductForegroundService(session, content_store=store, extractor=None)
    with pytest.raises(DomainRuleError) as cross_customer:
        service.edit(
            CUSTOMER_B,
            asset.id,
            base_version_id=base.id,
            base_manifest_sha256=base.manifest_sha256,
            mask_png=_mask(variant=1),
            editor_version="human-mask-editor-v1",
            edit_manifest_sha256="f" * 64,
            idempotency_key="foreign",
            operation_counts=MaskEditOperationCounts(1, 0, 0),
        )
    assert cross_customer.value.code == "foreground_asset_unavailable"
    other_asset = ProductAsset(
        id=uuid4(),
        customer_id=CUSTOMER_A,
        product_id="bracelet-2",
        photo_id=f"photo-{uuid4()}",
        identity="original_real_photo",
        confirmed=True,
        source_type="upload",
        quality_flags=[],
        occurred_at=datetime.now(UTC),
        content_sha256=asset.content_sha256,
        mime_type="image/png",
        width=10,
        height=10,
    )
    session.add(other_asset)
    session.commit()
    store.originals[(CUSTOMER_A, other_asset.id)] = store.originals[
        (CUSTOMER_A, asset.id)
    ]
    with pytest.raises(DomainRuleError) as cross_asset:
        service.edit(
            CUSTOMER_A,
            other_asset.id,
            base_version_id=base.id,
            base_manifest_sha256=base.manifest_sha256,
            mask_png=_mask(variant=1),
            editor_version="human-mask-editor-v1",
            edit_manifest_sha256="f" * 64,
            idempotency_key="cross-asset",
            operation_counts=MaskEditOperationCounts(1, 0, 0),
        )
    assert cross_asset.value.code == "foreground_asset_unavailable"
    with pytest.raises(DomainRuleError) as stale:
        _edit(service, asset, base, base_manifest="0" * 64)
    assert stale.value.code == "foreground_edit_stale"
    with pytest.raises(DomainRuleError) as tampered:
        _edit(service, asset, base, key="tampered", manifest="f" * 64)
    assert tampered.value.code == "foreground_edit_manifest_mismatch"


def test_edit_is_idempotent_for_same_manifest_and_conflicts_for_different_manifest(
    session: Session,
) -> None:
    store = MemoryContentStore()
    asset, base = _seed(session, store)
    service = ProductForegroundService(session, content_store=store, extractor=None)
    first = _edit(service, asset, base)
    replay = _edit(service, asset, base)
    assert replay.id == first.id
    assert replay.created is False
    with pytest.raises(DomainRuleError) as conflict:
        _edit(service, asset, base, mask=_mask(variant=2))
    assert conflict.value.code == "foreground_edit_conflict"


def test_two_tabs_receive_their_own_distinct_appended_versions(session: Session) -> None:
    store = MemoryContentStore()
    asset, base = _seed(session, store)
    service = ProductForegroundService(session, content_store=store, extractor=None)
    first = _edit(service, asset, base, key="tab-1", mask=_mask(variant=1))
    second = _edit(service, asset, base, key="tab-2", mask=_mask(variant=2))
    assert (first.version, second.version) == (2, 3)
    assert first.id != second.id


def test_edit_database_failure_discards_both_private_outputs(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryContentStore()
    asset, base = _seed(session, store)
    foreground_keys = set(store.foregrounds)
    mask_keys = set(store.masks)

    def fail_commit() -> None:
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        _edit(ProductForegroundService(session, content_store=store, extractor=None), asset, base)
    assert set(store.foregrounds) == foreground_keys
    assert set(store.masks) == mask_keys
