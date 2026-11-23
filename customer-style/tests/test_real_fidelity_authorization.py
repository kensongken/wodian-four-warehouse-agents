"""Durable single-dispatch authority for paid product fidelity checks."""
# ruff: noqa: F401, F811

from decimal import Decimal

import pytest
from sqlalchemy import select, update
from test_image_jobs_api import (
    CUSTOMER_A_ID,
    api_session_factory,
    asset_factory,
    content_store,
)
from test_style_suite_fidelity import _generated_suite

from opc_style.domain.authenticity import FidelityAssessment, FidelityBatchAssessment
from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    ProductIdentityCardVersion,
    RealFidelityRun,
    StyleSuiteCandidateFidelity,
    StyleSuiteRun,
)
from opc_style.services.real_fidelity import RealFidelityService

FIDELITY_COMPARISON_FIELDS = frozenset(
    {
        "category",
        "primary_colors",
        "visible_surface_appearance",
        "component_shapes",
        "visible_component_count",
        "component_arrangement",
        "core_accessories",
        "overall_form",
        "subject_completeness",
    }
)


class RecordingFidelityAssessor:
    assessor_version = "recording-fidelity-v1"

    def __init__(self, failure: str | None = None) -> None:
        self.calls = 0
        self.failure = failure
        self.last_request = None

    def assess_batch(self, request):
        self.calls += 1
        self.last_request = request
        if self.failure:
            raise DomainRuleError(self.failure)
        states = (
            ("red", ("product_category_changed",), "商品类别发生变化"),
            ("green", (), "商品身份一致"),
            ("unknown", ("provider_check_unavailable",), "暂时无法确认"),
        )
        comparisons = tuple(
            (field, "match") for field in sorted(FIDELITY_COMPARISON_FIELDS)
        )
        return FidelityBatchAssessment(
            assessments=tuple(
                FidelityAssessment(
                    state=state,
                    flags=flags,
                    assessor_version=self.assessor_version,
                    candidate_id=candidate.candidate_id,
                    summary_zh=summary,
                    comparisons=comparisons,
                )
                for candidate, (state, flags, summary) in zip(
                    request.candidates, states, strict=True
                )
            ),
            assessor_version=self.assessor_version,
        )


def test_fidelity_assessment_rejects_incomplete_comparisons() -> None:
    with pytest.raises(ValueError, match="fidelity_comparisons_invalid"):
        FidelityAssessment(
            state="green",
            flags=(),
            assessor_version="test-v1",
            summary_zh="商品身份一致",
            comparisons=(("category", "match"),),
        )


def _service(session, store, assessor) -> RealFidelityService:
    return RealFidelityService(
        session,
        content_store=store,
        assessor=assessor,
        model="gpt-4.1-mini-2025-04-14",
        maximum_usd=Decimal("0.50"),
    )


def test_preview_seals_four_images_without_provider_call(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset, real_model=True)
        preview = _service(session, content_store, assessor).preview(
            CUSTOMER_A_ID, run.id
        )

        assert preview.model == "gpt-4.1-mini-2025-04-14"
        assert preview.image_count == 4
        assert preview.candidate_count == 3
        assert preview.maximum_usd == Decimal("0.50")
        assert len(preview.input_manifest_sha256) == 64
        assert preview.attempt_state == "not_started"
        assert preview.publishable is False
        assert assessor.calls == 0
        assert session.scalar(select(RealFidelityRun)) is None


def test_preview_rejects_legacy_cardless_real_run_before_provider(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor()
    with api_session_factory() as session:
        legacy_run = _generated_suite(session, content_store, asset, real_model=True)
        session.execute(
            update(StyleSuiteRun)
            .where(StyleSuiteRun.id == legacy_run.id)
            .values(
                product_identity_card_version_id=None,
                product_identity_card_sha256=None,
            )
        )
        session.commit()

        with pytest.raises(
            DomainRuleError, match="confirmed_product_identity_card_required"
        ):
            _service(session, content_store, assessor).preview(
                CUSTOMER_A_ID, legacy_run.id
            )

    assert assessor.calls == 0


def test_wrong_quote_never_calls_provider(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset, real_model=True)
        service = _service(session, content_store, assessor)
        preview = service.preview(CUSTOMER_A_ID, run.id)

        with pytest.raises(DomainRuleError, match="real_fidelity_confirmation_required"):
            service.execute(
                CUSTOMER_A_ID,
                run.id,
                idempotency_key="wrong-cost",
                confirmed_maximum_usd=Decimal(0),
                confirmed_manifest_sha256=preview.input_manifest_sha256,
            )

    assert assessor.calls == 0


def test_execute_once_persists_results_and_replays_without_second_call(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor()
    with api_session_factory() as session:
        suite = _generated_suite(session, content_store, asset, real_model=True)
        service = _service(session, content_store, assessor)
        preview = service.preview(CUSTOMER_A_ID, suite.id)
        first = service.execute(
            CUSTOMER_A_ID,
            suite.id,
            idempotency_key="fidelity-round-1",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.input_manifest_sha256,
        )
        second = service.execute(
            CUSTOMER_A_ID,
            suite.id,
            idempotency_key="fidelity-round-1",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.input_manifest_sha256,
        )

        assert first.created is True
        assert second.created is False
        assert assessor.calls == 1
        assert assessor.last_request.product_sha256 == suite.source_content_sha256
        assert (
            assessor.last_request.identity_card_version_id
            == suite.product_identity_card_version_id
        )
        assert assessor.last_request.identity_card.category.value == "bracelet"
        assert len(assessor.last_request.candidates) == 3
        ledger = session.scalar(select(RealFidelityRun))
        assert ledger is not None
        card_manifest = [
            row
            for row in ledger.input_manifest
            if row["role"] == "confirmed_product_identity_card"
        ]
        assert card_manifest == [
            {
                "role": "confirmed_product_identity_card",
                "identity_card_version_id": str(
                    suite.product_identity_card_version_id
                ),
                "identity_card_sha256": suite.product_identity_card_sha256,
            }
        ]
        assert ledger.dispatch_state == "provider_responded"
        assert ledger.outbound_attempts == 1
        assert ledger.assessment_json is not None
        assert set(ledger.assessment_json[0]["comparisons"]) == set(
            FIDELITY_COMPARISON_FIELDS
        )
        fidelities = list(
            session.scalars(
                select(StyleSuiteCandidateFidelity).order_by(
                    StyleSuiteCandidateFidelity.image_candidate_id
                )
            )
        )
        assert len(fidelities) == 3
        assert {row.fidelity_state for row in fidelities} == {
            "green",
            "red",
            "unknown",
        }


def test_completed_replay_rereads_confirmed_card_without_second_provider_call(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor()
    with api_session_factory() as session:
        suite = _generated_suite(session, content_store, asset, real_model=True)
        service = _service(session, content_store, assessor)
        preview = service.preview(CUSTOMER_A_ID, suite.id)
        service.execute(
            CUSTOMER_A_ID,
            suite.id,
            idempotency_key="completed-card-replay",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.input_manifest_sha256,
        )
        card = session.get(
            ProductIdentityCardVersion,
            suite.product_identity_card_version_id,
        )
        assert card is not None
        changed = dict(card.card_payload)
        changed["category"] = {"value": "necklace", "certainty": "observed"}
        session.execute(
            update(ProductIdentityCardVersion)
            .where(ProductIdentityCardVersion.id == card.id)
            .values(card_payload=changed)
        )
        session.commit()

        with pytest.raises(DomainRuleError, match="product_identity_card_scope_mismatch"):
            service.execute(
                CUSTOMER_A_ID,
                suite.id,
                idempotency_key="completed-card-replay",
                confirmed_maximum_usd=preview.maximum_usd,
                confirmed_manifest_sha256=preview.input_manifest_sha256,
            )

    assert assessor.calls == 1


def test_different_idempotency_key_replays_same_suite_without_second_call(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor()
    with api_session_factory() as session:
        suite = _generated_suite(session, content_store, asset, real_model=True)
        service = _service(session, content_store, assessor)
        preview = service.preview(CUSTOMER_A_ID, suite.id)

        first = service.execute(
            CUSTOMER_A_ID,
            suite.id,
            idempotency_key="first-browser-request",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.input_manifest_sha256,
        )
        replay = service.execute(
            CUSTOMER_A_ID,
            suite.id,
            idempotency_key="different-browser-request",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.input_manifest_sha256,
        )

        ledgers = list(session.scalars(select(RealFidelityRun)))

    assert first.created is True
    assert replay.created is False
    assert assessor.calls == 1
    assert len(ledgers) == 1


def test_failed_dispatch_is_terminal_and_never_retried(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor(
        "fidelity_provider_http_400_invalid_request_error_invalid_json_schema"
    )
    with api_session_factory() as session:
        suite = _generated_suite(session, content_store, asset, real_model=True)
        service = _service(session, content_store, assessor)
        preview = service.preview(CUSTOMER_A_ID, suite.id)
        for _ in range(2):
            with pytest.raises(DomainRuleError, match="real_fidelity_outcome_uncertain"):
                service.execute(
                    CUSTOMER_A_ID,
                    suite.id,
                    idempotency_key="failed-fidelity-round",
                    confirmed_maximum_usd=preview.maximum_usd,
                    confirmed_manifest_sha256=preview.input_manifest_sha256,
                )

        ledger = session.scalar(select(RealFidelityRun))
        assert ledger is not None
        assert ledger.failure_code == (
            "fidelity_provider_http_400_invalid_request_error_invalid_json_schema"
        )

    assert assessor.calls == 1


def test_failed_dispatch_preview_exposes_terminal_attempt_state(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor("fidelity_provider_response_invalid")
    with api_session_factory() as session:
        suite = _generated_suite(session, content_store, asset, real_model=True)
        service = _service(session, content_store, assessor)
        preview = service.preview(CUSTOMER_A_ID, suite.id)
        with pytest.raises(DomainRuleError, match="real_fidelity_outcome_uncertain"):
            service.execute(
                CUSTOMER_A_ID,
                suite.id,
                idempotency_key="failed-preview-state",
                confirmed_maximum_usd=preview.maximum_usd,
                confirmed_manifest_sha256=preview.input_manifest_sha256,
            )

        restored = service.preview(CUSTOMER_A_ID, suite.id)

    assert restored.attempt_state == "dispatch_uncertain"
    assert assessor.calls == 1


def test_failed_dispatch_with_new_key_never_calls_provider_again(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor("fidelity_provider_response_invalid")
    with api_session_factory() as session:
        suite = _generated_suite(session, content_store, asset, real_model=True)
        service = _service(session, content_store, assessor)
        preview = service.preview(CUSTOMER_A_ID, suite.id)
        for key in ("failed-first-key", "failed-different-key"):
            with pytest.raises(DomainRuleError, match="real_fidelity_outcome_uncertain"):
                service.execute(
                    CUSTOMER_A_ID,
                    suite.id,
                    idempotency_key=key,
                    confirmed_maximum_usd=preview.maximum_usd,
                    confirmed_manifest_sha256=preview.input_manifest_sha256,
                )

        ledgers = list(session.scalars(select(RealFidelityRun)))

    assert assessor.calls == 1
    assert len(ledgers) == 1


def test_preview_rejects_stale_card_seal_before_provider(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor()
    with api_session_factory() as session:
        suite = _generated_suite(session, content_store, asset, real_model=True)
        session.execute(
            update(StyleSuiteRun)
            .where(StyleSuiteRun.id == suite.id)
            .values(product_identity_card_sha256="f" * 64)
        )
        session.commit()

        with pytest.raises(DomainRuleError, match="product_identity_card_scope_mismatch"):
            _service(session, content_store, assessor).preview(CUSTOMER_A_ID, suite.id)

    assert assessor.calls == 0


def test_execute_rereads_card_immediately_before_dispatch(
    api_session_factory, content_store, asset_factory
) -> None:
    asset = asset_factory()
    assessor = RecordingFidelityAssessor()
    with api_session_factory() as session:
        suite = _generated_suite(session, content_store, asset, real_model=True)
        service = _service(session, content_store, assessor)
        preview = service.preview(CUSTOMER_A_ID, suite.id)
        original_prepare = service._prepare
        execute_prepare_calls = 0

        def tamper_after_first_execute_prepare(customer_id, suite_run_id):
            nonlocal execute_prepare_calls
            prepared = original_prepare(customer_id, suite_run_id)
            execute_prepare_calls += 1
            if execute_prepare_calls == 1:
                card = session.get(
                    ProductIdentityCardVersion,
                    suite.product_identity_card_version_id,
                )
                assert card is not None
                changed = dict(card.card_payload)
                changed["category"] = {"value": "necklace", "certainty": "observed"}
                session.execute(
                    update(ProductIdentityCardVersion)
                    .where(ProductIdentityCardVersion.id == card.id)
                    .values(card_payload=changed)
                )
                session.commit()
            return prepared

        service._prepare = tamper_after_first_execute_prepare  # type: ignore[method-assign]

        with pytest.raises(DomainRuleError, match="product_identity_card_scope_mismatch"):
            service.execute(
                CUSTOMER_A_ID,
                suite.id,
                idempotency_key="card-changed-before-dispatch",
                confirmed_maximum_usd=preview.maximum_usd,
                confirmed_manifest_sha256=preview.input_manifest_sha256,
            )

        ledger = session.scalar(select(RealFidelityRun))
        assert ledger is not None
        assert ledger.dispatch_state == "reserved"
        assert ledger.outbound_attempts == 0

    assert assessor.calls == 0
