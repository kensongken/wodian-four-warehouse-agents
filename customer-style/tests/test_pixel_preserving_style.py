from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.background_generator import (
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    GeneratedBackground,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_foreground import ForegroundTransform
from opc_style.models import (
    Customer,
    ImageCandidate,
    ImageOutputJob,
    PixelBackgroundRun,
    PixelCompositionReceipt,
    ProductAsset,
    ProductForegroundVersion,
    StyleCard,
    StyleCardVersion,
    StyleReferencePack,
    StyleReferencePackItem,
    StyleSample,
    StyleSuiteRun,
)
from opc_style.services.pixel_preserving_style import PixelPreservingStyleService
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.settings import RealBackgroundProviderSettings


def _png(color: str, *, rgba: bool = False) -> bytes:
    output = BytesIO()
    Image.new("RGBA" if rgba else "RGB", (64, 64), color).save(output, "PNG")
    return output.getvalue()


def _foreground() -> tuple[bytes, bytes]:
    foreground = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(foreground)
    draw.rectangle((8, 8, 23, 23), fill=(200, 20, 20, 255))
    mask = foreground.getchannel("A")
    foreground_out, mask_out = BytesIO(), BytesIO()
    foreground.save(foreground_out, "PNG")
    mask.save(mask_out, "PNG")
    return foreground_out.getvalue(), mask_out.getvalue()


class FakeBackgroundGenerator:
    adapter_version = "fake-background-v1"
    model = "gpt-image-2"
    publishable = False

    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[BackgroundGenerationRequest] = []

    def generate(self, request: BackgroundGenerationRequest) -> GeneratedBackground:
        self.calls.append(request)
        if self.failure:
            raise BackgroundGenerationError(self.failure, outbound_attempts=1)
        content = _png("beige")
        return GeneratedBackground(
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/png",
            width=64,
            height=64,
            metadata={"fake": True},
        )


class ShallowTabletopBackgroundGenerator(FakeBackgroundGenerator):
    def generate(self, request: BackgroundGenerationRequest) -> GeneratedBackground:
        self.calls.append(request)
        image = Image.new("RGB", (64, 64), (170, 70, 54))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 52, 63, 63), fill=(238, 224, 206))
        output = BytesIO()
        image.save(output, "PNG")
        content = output.getvalue()
        return GeneratedBackground(
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/png",
            width=64,
            height=64,
            metadata={"fake": True, "tabletop": "shallow"},
        )


def _seed(session: Session, store: LocalSyntheticAssetStore) -> dict[str, UUID]:
    customer_id, other_customer_id = uuid4(), uuid4()
    session.add_all(
        (
            Customer(id=customer_id, display_name="Pixel owner"),
            Customer(id=other_customer_id, display_name="Other"),
        )
    )
    source = _png("white")
    asset_id = uuid4()
    session.add(
        ProductAsset(
            id=asset_id,
            customer_id=customer_id,
            product_id="bracelet-1",
            photo_id="photo-1",
            identity="original_real_photo",
            confirmed=True,
            source_type="upload",
            quality_flags=[],
            occurred_at=datetime.now(UTC),
            content_sha256=sha256(source).hexdigest(),
            mime_type="image/png",
            width=64,
            height=64,
        )
    )
    suite_id = uuid4()
    session.add(
        StyleSuiteRun(
            id=suite_id,
            customer_id=customer_id,
            product_id="bracelet-1",
            source_asset_id=asset_id,
            source_content_sha256=sha256(source).hexdigest(),
            style_count=1,
            slot_count=1,
            child_job_count=1,
            candidate_count=1,
            candidate_estimate_usd=Decimal("0.12"),
            maximum_estimated_cost_usd=Decimal("0.12"),
            cost_basis="local_planning_estimate",
            currency="USD",
            real_generation_enabled=True,
            candidate_count_per_item=1,
            idempotency_key=f"suite-{uuid4()}",
            state="planned",
        )
    )
    foreground_png, mask_png = _foreground()
    foreground_id = uuid4()
    session.add(
        ProductForegroundVersion(
            id=foreground_id,
            customer_id=customer_id,
            product_asset_id=asset_id,
            product_id="bracelet-1",
            version=1,
            source_content_sha256=sha256(source).hexdigest(),
            foreground_sha256=sha256(foreground_png).hexdigest(),
            mask_sha256=sha256(mask_png).hexdigest(),
            width=32,
            height=32,
            extractor_version="fake-v1",
            manifest_sha256="f" * 64,
            pipeline_manifest_sha256="e" * 64,
            stage_receipts_sha256="d" * 64,
            state="confirmed",
            quality_flags=[],
            confirmation_key="confirm-fg",
            decided_at=datetime.now(UTC),
        )
    )
    store.persist_product_foreground(
        customer_id, foreground_id, foreground_png, sha256(foreground_png).hexdigest()
    )
    store.persist_product_mask(
        customer_id, foreground_id, mask_png, sha256(mask_png).hexdigest()
    )

    card = StyleCard(id=uuid4(), customer_id=customer_id, kind="scene")
    version = StyleCardVersion(
        id=uuid4(),
        customer_id=customer_id,
        style_card_id=card.id,
        version=1,
        content={
            "historical_style": True,
            "stability": "customer_confirmed",
            "style_origin": "customer_upload",
        },
    )
    session.add_all((card, version))
    session.flush()
    samples: list[StyleSample] = []
    for index, color in enumerate(("red", "blue"), start=1):
        content = _png(color)
        sample = StyleSample(
            id=uuid4(),
            customer_id=customer_id,
            photo_id=f"style-{index}",
            authorized_at=datetime.now(UTC),
            consent_provenance="customer_upload",
            consent_text_version="v1",
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/png",
            width=64,
            height=64,
            asset_store_namespace=store.store_namespace,
        )
        session.add(sample)
        samples.append(sample)
        store.persist_style_sample_content(
            customer_id, sample.id, content, sample.content_sha256
        )
    session.flush()
    from opc_style.domain.style_reference_pack import (
        StyleReference,
        reference_manifest_sha256,
    )

    references = tuple(
        StyleReference(
            sample_id=sample.id,
            content_sha256=sample.content_sha256,
            mime_type="image/png",
        )
        for sample in samples
    )
    pack_id = uuid4()
    pack = StyleReferencePack(
        id=pack_id,
        customer_id=customer_id,
        scene_style_version_id=version.id,
        selection_version="test-v1",
        manifest_sha256=reference_manifest_sha256(references),
        confirmed_by=str(customer_id),
        active=True,
    )
    session.add(pack)
    session.flush()
    session.add_all(
        StyleReferencePackItem(
            id=uuid4(),
            customer_id=customer_id,
            pack_id=pack.id,
            style_sample_id=sample.id,
            position=position,
            content_sha256=sample.content_sha256,
            mime_type="image/png",
        )
        for position, sample in enumerate(samples)
    )
    job_id = uuid4()
    session.add(
        ImageOutputJob(
            id=job_id,
            customer_id=customer_id,
            output_type="product_style",
            style_card_version_id=None,
            scene_style_id=None,
            output_ratio="1:1",
            adapter_version="pixel-preserving-v1",
            status="awaiting_review",
            request_metadata={"publishable": False},
            idempotency_key=f"job-{uuid4()}",
        )
    )
    session.commit()
    return {
        "customer": customer_id,
        "other": other_customer_id,
        "suite": suite_id,
        "foreground": foreground_id,
        "pack": pack_id,
        "job": job_id,
    }


@pytest.fixture
def pixel_scope(session: Session, tmp_path: Path):
    store = LocalSyntheticAssetStore(tmp_path / "pixel-store")
    ids = _seed(session, store)
    yield store, ids
    store.close()


def _service(session: Session, store, generator=None) -> PixelPreservingStyleService:
    return PixelPreservingStyleService(
        session,
        content_store=store,
        settings=RealBackgroundProviderSettings(
            enabled=True, api_key="test", max_run_usd=Decimal("0.12")
        ),
        generator=generator or FakeBackgroundGenerator(),
    )


def _preview(service: PixelPreservingStyleService, ids: dict[str, UUID]):
    return service.preview(
        ids["customer"],
        style_suite_run_id=ids["suite"],
        foreground_version_id=ids["foreground"],
        style_reference_pack_id=ids["pack"],
        slot_rules={"props": "none", "presentation": "isolated"},
        style_rules={"lighting": "soft"},
        output_size=(64, 64),
    )


def test_preview_seals_scope_and_rejects_cross_customer(
    session: Session, pixel_scope
) -> None:
    store, ids = pixel_scope
    service = _service(session, store)
    preview = _preview(service, ids)
    assert preview.authorized_manifest["product_category"] == "legacy_unspecified"
    assert preview.provider_image_count == 2
    assert preview.maximum_usd == Decimal("0.12")
    assert preview.publishable is False
    assert preview.authorized_manifest["foreground_manifest_sha256"] == "f" * 64
    with pytest.raises(DomainRuleError, match="pixel_background_scope_mismatch"):
        service.preview(
            ids["other"],
            style_suite_run_id=ids["suite"],
            foreground_version_id=ids["foreground"],
            style_reference_pack_id=ids["pack"],
            slot_rules={},
            style_rules={},
        )


def test_execution_is_one_attempt_idempotent_and_product_blind(
    session: Session, pixel_scope
) -> None:
    store, ids = pixel_scope
    generator = FakeBackgroundGenerator()
    service = _service(session, store, generator)
    preview = _preview(service, ids)
    outcome = service.execute_background(
        ids["customer"],
        preview,
        idempotency_key="paid-once",
        confirmed_maximum_usd=Decimal("0.12"),
    )
    replay = service.execute_background(
        ids["customer"],
        preview,
        idempotency_key="paid-once",
        confirmed_maximum_usd=Decimal("0.12"),
    )
    assert outcome.outbound_attempts == 1
    assert outcome.review_state == "awaiting_review"
    assert (outcome.output_width, outcome.output_height) == (64, 64)
    assert replay.id == outcome.id and replay.created is False
    assert len(generator.calls) == 1
    assert not hasattr(generator.calls[0], "foreground_sha256")
    request_facts = repr(generator.calls[0])
    for private_fact in (
        "source_content",
        "foreground_png",
        "foreground_sha256",
        "mask_png",
        "mask_sha256",
        "identity_card",
        "pipeline_manifest_sha256",
    ):
        assert private_fact not in request_facts
    assert store.verify_pixel_background(
        ids["customer"], outcome.id, outcome.result_sha256 or ""
    )


def test_budget_mismatch_and_uncertain_dispatch_fail_closed(
    session: Session, pixel_scope
) -> None:
    store, ids = pixel_scope
    generator = FakeBackgroundGenerator(failure="background_dispatch_uncertain")
    service = _service(session, store, generator)
    preview = _preview(service, ids)
    with pytest.raises(DomainRuleError, match="pixel_background_budget_mismatch"):
        service.execute_background(
            ids["customer"],
            preview,
            idempotency_key="wrong-cost",
            confirmed_maximum_usd=Decimal("0.11"),
        )
    with pytest.raises(DomainRuleError, match="background_dispatch_uncertain"):
        service.execute_background(
            ids["customer"],
            preview,
            idempotency_key="unknown-outcome",
            confirmed_maximum_usd=Decimal("0.12"),
        )
    row = session.scalar(
        select(PixelBackgroundRun).where(
            PixelBackgroundRun.idempotency_key == "unknown-outcome"
        )
    )
    assert row is not None
    assert row.dispatch_state == "dispatch_uncertain" and row.outbound_attempts == 1
    replay = service.execute_background(
        ids["customer"],
        preview,
        idempotency_key="unknown-outcome",
        confirmed_maximum_usd=Decimal("0.12"),
    )
    assert replay.dispatch_state == "dispatch_uncertain"
    assert len(generator.calls) == 1


def test_withdrawn_reference_after_preview_blocks_dispatch(
    session: Session, pixel_scope
) -> None:
    store, ids = pixel_scope
    generator = FakeBackgroundGenerator()
    service = _service(session, store, generator)
    preview = _preview(service, ids)
    sample = session.scalar(select(StyleSample).limit(1))
    assert sample is not None
    sample.withdrawn_at = datetime.now(UTC)
    session.commit()
    with pytest.raises(DomainRuleError, match="style_reference_pack_unavailable"):
        service.execute_background(
            ids["customer"],
            preview,
            idempotency_key="withdrawn",
            confirmed_maximum_usd=Decimal("0.12"),
        )
    assert generator.calls == []


def test_only_confirmed_background_composes_and_receipt_is_not_publishable(
    session: Session, pixel_scope
) -> None:
    store, ids = pixel_scope
    service = _service(session, store)
    preview = _preview(service, ids)
    outcome = service.execute_background(
        ids["customer"],
        preview,
        idempotency_key="compose-once",
        confirmed_maximum_usd=Decimal("0.12"),
    )
    with pytest.raises(DomainRuleError, match="confirmed_pixel_background_required"):
        service.compose(
            ids["customer"],
            outcome.id,
            output_job_id=ids["job"],
            transform=ForegroundTransform(0, 0, Decimal(1), Decimal(0)),
            output_size=(64, 64),
        )
    service.confirm_background(ids["customer"], outcome.id, decision="confirmed")
    with pytest.raises(DomainRuleError, match="pixel_composition_size_mismatch"):
        service.compose(
            ids["customer"],
            outcome.id,
            output_job_id=ids["job"],
            transform=ForegroundTransform(0, 0, Decimal(1), Decimal(0)),
            output_size=(64, 48),
        )
    composed = service.compose(
        ids["customer"],
        outcome.id,
        output_job_id=ids["job"],
        transform=ForegroundTransform(0, 0, Decimal(1), Decimal(0)),
        output_size=(64, 64),
    )
    candidate = session.get(ImageCandidate, composed.candidate_id)
    receipt = session.get(PixelCompositionReceipt, composed.receipt_id)
    assert candidate is not None and candidate.artifact_metadata["publishable"] is False
    assert receipt is not None and receipt.publishable is False
    assert receipt.verification_state == "verified"


def test_shallow_tabletop_is_reported_and_cannot_be_saved(
    session: Session, pixel_scope
) -> None:
    store, ids = pixel_scope
    service = _service(session, store, ShallowTabletopBackgroundGenerator())
    preview = _preview(service, ids)
    outcome = service.execute_background(
        ids["customer"],
        preview,
        idempotency_key="shallow-tabletop",
        confirmed_maximum_usd=Decimal("0.12"),
    )
    service.confirm_background(ids["customer"], outcome.id, decision="confirmed")

    recommendation = service.recommend_composition(ids["customer"], outcome.id)
    assert recommendation.save_allowed is False
    assert recommendation.placement_state == "background_unsuitable"
    assert recommendation.tabletop_depth_ratio < Decimal("0.2800")
    with pytest.raises(DomainRuleError, match="pixel_background_tabletop_insufficient"):
        service.compose(
            ids["customer"],
            outcome.id,
            output_job_id=ids["job"],
            transform=ForegroundTransform(0, 0, Decimal("0.20"), Decimal(0)),
            output_size=(64, 64),
        )


def test_pixel_composition_requires_confirmed_pipeline_foreground(
    session: Session, pixel_scope
) -> None:
    store, ids = pixel_scope
    service = _service(session, store)
    preview = _preview(service, ids)
    outcome = service.execute_background(
        ids["customer"],
        preview,
        idempotency_key="pipeline-required",
        confirmed_maximum_usd=Decimal("0.12"),
    )
    service.confirm_background(ids["customer"], outcome.id, decision="confirmed")
    foreground = session.get(ProductForegroundVersion, ids["foreground"])
    assert foreground is not None
    foreground.pipeline_manifest_sha256 = None
    foreground.stage_receipts_sha256 = None
    session.commit()

    with pytest.raises(DomainRuleError, match="foreground_pipeline_evidence_required"):
        service.compose(
            ids["customer"],
            outcome.id,
            output_job_id=ids["job"],
            transform=ForegroundTransform(0, 0, Decimal(1), Decimal(0)),
            output_size=(64, 64),
        )


def test_background_request_never_contains_product_source_foreground_mask_or_identity(
    session: Session, pixel_scope
) -> None:
    store, ids = pixel_scope
    generator = FakeBackgroundGenerator()
    service = _service(session, store, generator)
    preview = _preview(service, ids)
    service.execute_background(
        ids["customer"],
        preview,
        idempotency_key="product-blind-boundary",
        confirmed_maximum_usd=Decimal("0.12"),
    )

    request = generator.calls[0]
    assert len(request.references) == 2
    assert request.slot_rules == {"props": "none", "presentation": "isolated"}
    assert request.style_rules == {"lighting": "soft"}
    assert request.output_size == (64, 64)
    forbidden = json.dumps(request.slot_rules) + json.dumps(request.style_rules)
    assert "product" not in forbidden.lower()
    assert "identity" not in forbidden.lower()


def test_rejected_background_cannot_compose(session: Session, pixel_scope) -> None:
    store, ids = pixel_scope
    service = _service(session, store)
    preview = _preview(service, ids)
    outcome = service.execute_background(
        ids["customer"],
        preview,
        idempotency_key="reject-once",
        confirmed_maximum_usd=Decimal("0.12"),
    )
    service.confirm_background(ids["customer"], outcome.id, decision="rejected")
    with pytest.raises(DomainRuleError, match="confirmed_pixel_background_required"):
        service.compose(
            ids["customer"],
            outcome.id,
            output_job_id=ids["job"],
            transform=ForegroundTransform(0, 0, Decimal(1), Decimal(0)),
            output_size=(64, 64),
        )


def test_migrated_database_accepts_exact_one_attempt_and_receipt_transitions(
    migrated_session: Session, tmp_path: Path
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "migrated-pixel-store")
    try:
        ids = _seed(migrated_session, store)
        service = _service(migrated_session, store)
        preview = _preview(service, ids)
        outcome = service.execute_background(
            ids["customer"],
            preview,
            idempotency_key="migrated-once",
            confirmed_maximum_usd=Decimal("0.12"),
        )
        service.confirm_background(ids["customer"], outcome.id, decision="confirmed")
        composed = service.compose(
            ids["customer"],
            outcome.id,
            output_job_id=ids["job"],
            transform=ForegroundTransform(0, 0, Decimal(1), Decimal(0)),
            output_size=(64, 64),
        )
        assert migrated_session.get(PixelCompositionReceipt, composed.receipt_id)
    finally:
        store.close()


def test_pixel_artifact_store_is_customer_scoped_and_restart_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "restart-pixel-store"
    customer_id, other_customer_id, run_id = uuid4(), uuid4(), uuid4()
    content = _png("gray")
    digest = sha256(content).hexdigest()
    store = LocalSyntheticAssetStore(root)
    store.persist_pixel_background(customer_id, run_id, content, digest)
    assert not store.verify_pixel_background(other_customer_id, run_id, digest)
    store.close()

    reopened = LocalSyntheticAssetStore(root, reopen=True)
    try:
        assert reopened.read_pixel_background(customer_id, run_id, digest) == content
    finally:
        reopened.close()
