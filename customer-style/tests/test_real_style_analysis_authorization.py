"""Durable single-dispatch authority for paid visual style analysis."""

from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select

from opc_style.domain import analyzer as contracts
from opc_style.domain.errors import DomainRuleError
from opc_style.models import RealStyleAnalysisRun
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.services.real_style_analysis import (
    RealStyleAnalysisService,
    latest_successful_real_style_analysis,
)
from opc_style.services.samples import StyleSampleService


def _png(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


def _library(session, store, customer_id) -> None:
    for index in range(10):
        content = _png(((index * 31) % 255, (index * 61) % 255, (index * 91) % 255))
        StyleSampleService(session, store).authorize_content(
            customer_id,
            photo_id=f"paid-style-{index}",
            content=content,
            claimed_sha256=sha256(content).hexdigest(),
            mime_type="image/png",
            consent_provenance="local-explicit-style-upload",
            consent_text_version="local-style-upload-v1",
        )


class RecordingAnalyzer(contracts.DeterministicPixelStyleAnalyzer):
    analyzer_version = "mock-real-visual-v1"
    analysis_mode = "real_visual_analysis_proposal"

    def __init__(self, failure: str | None = None) -> None:
        self.calls = 0
        self.failure = failure

    def analyze(self, samples):
        self.calls += 1
        if self.failure:
            raise DomainRuleError(self.failure)
        return super().analyze(samples)


def _service(session, store, analyzer) -> RealStyleAnalysisService:
    return RealStyleAnalysisService(
        session,
        content_store=store,
        analyzer=analyzer,
        model="gpt-4.1-mini-2025-04-14",
        maximum_usd=Decimal("0.422722"),
    )


def test_preview_never_calls_provider_and_seals_scope(session, tmp_path):
    customer_id = uuid4()
    analyzer = RecordingAnalyzer()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        _library(session, store, customer_id)
        preview = _service(session, store, analyzer).preview(customer_id)

    assert preview.library_photo_count == 10
    assert preview.selected_photo_count == 8
    assert preview.maximum_usd == Decimal("0.422722")
    assert preview.publishable is False
    assert len(preview.reference_manifest_sha256) == 64
    assert analyzer.calls == 0
    assert session.scalar(select(func.count()).select_from(RealStyleAnalysisRun)) == 0


def test_execute_once_then_replay_without_second_provider_call(session, tmp_path):
    customer_id = uuid4()
    analyzer = RecordingAnalyzer()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        _library(session, store, customer_id)
        service = _service(session, store, analyzer)
        preview = service.preview(customer_id)
        first = service.execute(
            customer_id,
            idempotency_key="analysis-round-1",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_reference_manifest_sha256=preview.reference_manifest_sha256,
        )
        second = service.execute(
            customer_id,
            idempotency_key="analysis-round-1",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_reference_manifest_sha256=preview.reference_manifest_sha256,
        )

    assert first.created is True
    assert second.created is False
    assert first.proposal == second.proposal
    assert analyzer.calls == 1
    run = session.scalar(select(RealStyleAnalysisRun))
    assert run is not None
    assert run.dispatch_state == "provider_responded"
    assert run.outbound_attempts == 1


def test_latest_successful_result_recovers_without_provider_call(session, tmp_path):
    customer_id = uuid4()
    analyzer = RecordingAnalyzer()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        _library(session, store, customer_id)
        service = _service(session, store, analyzer)
        preview = service.preview(customer_id)
        created = service.execute(
            customer_id,
            idempotency_key="recoverable-analysis",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_reference_manifest_sha256=preview.reference_manifest_sha256,
        )
        recovered = latest_successful_real_style_analysis(session, customer_id)

    assert recovered is not None
    assert recovered.run_id == created.run_id
    assert recovered.proposal == created.proposal
    assert recovered.created is False
    assert analyzer.calls == 1


def test_latest_successful_result_ignores_uncertain_run(session, tmp_path):
    customer_id = uuid4()
    analyzer = RecordingAnalyzer("style_visual_analysis_timeout")
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        _library(session, store, customer_id)
        service = _service(session, store, analyzer)
        preview = service.preview(customer_id)
        with pytest.raises(DomainRuleError, match="real_style_analysis_outcome_uncertain"):
            service.execute(
                customer_id,
                idempotency_key="uncertain-analysis",
                confirmed_maximum_usd=preview.maximum_usd,
                confirmed_reference_manifest_sha256=preview.reference_manifest_sha256,
            )

    assert latest_successful_real_style_analysis(session, customer_id) is None


def test_wrong_cost_or_manifest_never_calls_provider(session, tmp_path):
    customer_id = uuid4()
    analyzer = RecordingAnalyzer()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        _library(session, store, customer_id)
        service = _service(session, store, analyzer)
        preview = service.preview(customer_id)
        for cost, manifest in (
            (Decimal("0.40"), preview.reference_manifest_sha256),
            (preview.maximum_usd, "f" * 64),
        ):
            with pytest.raises(DomainRuleError, match="real_style_analysis_confirmation_required"):
                service.execute(
                    customer_id,
                    idempotency_key=f"bad-{cost}-{manifest[0]}",
                    confirmed_maximum_usd=cost,
                    confirmed_reference_manifest_sha256=manifest,
                )
    assert analyzer.calls == 0


def test_provider_failure_is_terminal_and_never_auto_retries(session, tmp_path):
    customer_id = uuid4()
    analyzer = RecordingAnalyzer("style_visual_analysis_timeout")
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        _library(session, store, customer_id)
        service = _service(session, store, analyzer)
        preview = service.preview(customer_id)
        for _ in range(2):
            with pytest.raises(DomainRuleError, match="real_style_analysis_outcome_uncertain"):
                service.execute(
                    customer_id,
                    idempotency_key="failed-round",
                    confirmed_maximum_usd=preview.maximum_usd,
                    confirmed_reference_manifest_sha256=preview.reference_manifest_sha256,
                )
    assert analyzer.calls == 1


def test_pre_dispatch_budget_rejection_is_not_recorded_as_outbound(
    session, tmp_path
):
    customer_id = uuid4()
    failure_code = "style_visual_analysis_budget_exhausted"
    analyzer = RecordingAnalyzer(failure_code)
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        _library(session, store, customer_id)
        service = _service(session, store, analyzer)
        preview = service.preview(customer_id)
        for _ in range(2):
            with pytest.raises(
                DomainRuleError, match="real_style_analysis_budget_exhausted"
            ):
                service.execute(
                    customer_id,
                    idempotency_key="definitive-rejection",
                    confirmed_maximum_usd=preview.maximum_usd,
                    confirmed_reference_manifest_sha256=preview.reference_manifest_sha256,
                )
    assert analyzer.calls == 1
    run = session.scalar(select(RealStyleAnalysisRun))
    assert run is not None
    assert run.dispatch_state == "reserved"
    assert run.outbound_attempts == 0
    assert run.failure_code == failure_code
