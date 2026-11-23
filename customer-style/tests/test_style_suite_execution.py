"""A suite run executes and replays the complete sealed style/slot matrix."""
# ruff: noqa: F811

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from test_image_jobs_api import (  # noqa: F401
    CUSTOMER_A_ID,
    CUSTOMER_B_ID,
    FailSecondPublishStore,
    api_session_factory,
    asset_factory,
    content_store,
)
from test_openai_image_provider import _settings, _success_payload
from test_product_style_output import (
    _confirmed_identity_card,
    _confirmed_identity_observation,
    _historical,
)

from alembic import command
from opc_style.db import create_database_engine
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import ImageGenerationError
from opc_style.models import (
    AuditEvent,
    ImageOutputJob,
    ProductAssetLink,
    StyleSuiteRun,
    StyleSuiteRunItem,
)
from opc_style.providers.openai_images import OpenAIImageGenerator, RealImageBudget
from opc_style.schemas import StyleSuitePilotScope
from opc_style.services.image_jobs import (
    ImageGenerationOutcomeUncertainError,
    ImageJobService,
)
from opc_style.services.real_pilot_authorization import (
    issue_real_pilot_authorization,
)
from opc_style.services.style_suites import StyleSuiteService
from opc_style.settings import StyleSuitePlanningSettings


def _service(session: Any, store: Any, generator: Any) -> StyleSuiteService:
    return StyleSuiteService(
        session,
        settings=StyleSuitePlanningSettings(
            candidate_estimate_usd=Decimal("0.12"), real_generation_enabled=True
        ),
        content_store=store,
        generator=generator,
    )


def _pilot_authorization(
    asset: Any, scene_style_version_id: Any, idempotency_key: str
) -> Any:
    return issue_real_pilot_authorization(
        customer_id=CUSTOMER_A_ID,
        product_id=asset.product_id,
        source_asset_id=asset.id,
        scene_style_version_id=scene_style_version_id,
        slot_key="detail",
        idempotency_key=idempotency_key,
        authorized_usd=Decimal("1.00"),
    )


def test_full_matrix_replay_is_one_child_per_style_slot(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    from test_classification_bridge_api import _real_generator

    calls = []
    generator = _real_generator(calls)
    asset = asset_factory()
    with api_session_factory() as session:
        _historical(session, content_store, name="warm")
        _historical(session, content_store, name="white")
        card = _confirmed_identity_card(session, asset)
        service = _service(session, content_store, generator)
        first = service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-full",
            product_identity_card_version_id=card.id,
        )
        second = service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-full",
            product_identity_card_version_id=card.id,
        )
        assert first.id == second.id
        assert first.state == "awaiting_review"
        assert first.child_job_count == 2
        assert first.candidate_count == 6
        assert len(calls) == 2
        assert all(item.candidate_count == 3 for item in first.items)
        assert all(item.output_job_id for item in first.items)
        assert session.scalar(select(StyleSuiteRun).where(StyleSuiteRun.id == first.id))
        assert len(list(session.scalars(select(StyleSuiteRunItem)))) == 2
        links = list(session.scalars(select(ProductAssetLink)))
        assert len(links) == 2
        assert {link.product_asset_id for link in links} == {asset.id}
        jobs = list(session.scalars(select(ImageOutputJob)))
        assert {job.idempotency_key for job in jobs} == {
            f"suite:{first.id}:{item.scene_style_version_id}:"
            f"{item.slot_key}:{first.source_content_sha256}"
            for item in first.items
        }
        assert all(job.request_metadata["publishable"] is False for job in jobs)


def test_pilot_scope_replay_stays_at_one_sealed_style_slot(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A replay retains its original single-item scope after new styles appear."""
    from test_classification_bridge_api import _real_generator

    provider_calls = []
    asset = asset_factory()
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="warm")
        card = _confirmed_identity_card(session, asset)
        scope = StyleSuitePilotScope(
            scene_style_version_id=selected.version.id,
            slot_key="detail",
        )
        service = _service(session, content_store, _real_generator(provider_calls))
        first = service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-pilot",
            pilot_scope=scope,
            product_identity_card_version_id=card.id,
        )
        _historical(session, content_store, name="later")
        replay = service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-pilot",
            pilot_scope=scope,
            product_identity_card_version_id=card.id,
        )

        assert replay.id == first.id
        assert replay.child_job_count == 1
        assert replay.candidate_count == 3
        assert [
            (item.scene_style_version_id, item.slot_key) for item in replay.items
        ] == [(selected.version.id, "detail")]
    assert len(provider_calls) == 1


def test_pilot_scope_replay_rejects_a_different_scope(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A reused key cannot substitute another confirmed style into a paid run."""
    from test_classification_bridge_api import _real_generator

    provider_calls = []
    asset = asset_factory()
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="warm")
        replacement, _ = _historical(session, content_store, name="white")
        card = _confirmed_identity_card(session, asset)
        service = _service(session, content_store, _real_generator(provider_calls))
        service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="scope-change",
            pilot_scope=StyleSuitePilotScope(
                scene_style_version_id=selected.version.id,
                slot_key="detail",
            ),
            product_identity_card_version_id=card.id,
        )

        with pytest.raises(DomainRuleError, match="style_suite_idempotency_conflict"):
            service.create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key="scope-change",
                pilot_scope=StyleSuitePilotScope(
                    scene_style_version_id=replacement.version.id,
                    slot_key="detail",
                ),
                product_identity_card_version_id=card.id,
            )
    assert len(provider_calls) == 1


def test_scoped_run_rejects_unscoped_idempotency_replay(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A scoped first request cannot be reopened as an unscoped matrix."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="warm")
        service = _service(session, content_store, _real_generator([]))
        service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="scoped-to-full",
            pilot_scope=StyleSuitePilotScope(
                scene_style_version_id=selected.version.id,
                slot_key="detail",
            ),
        )

        with pytest.raises(DomainRuleError, match="style_suite_idempotency_conflict"):
            service.create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key="scoped-to-full",
            )


def test_unscoped_run_rejects_scoped_idempotency_replay(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A full-matrix first request cannot be narrowed by a later replay."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="warm")
        service = _service(session, content_store, _real_generator([]))
        service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="full-to-scoped",
        )

        with pytest.raises(DomainRuleError, match="style_suite_idempotency_conflict"):
            service.create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key="full-to-scoped",
                pilot_scope=StyleSuitePilotScope(
                    scene_style_version_id=selected.version.id,
                    slot_key="detail",
                ),
            )


def test_race_recovery_rejects_changed_pilot_scope(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unique-key recovery path must enforce the same sealed scope."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="warm")
        replacement, _ = _historical(session, content_store, name="white")
        service = _service(session, content_store, _real_generator([]))
        service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="race-scope-change",
            pilot_scope=StyleSuitePilotScope(
                scene_style_version_id=selected.version.id,
                slot_key="detail",
            ),
        )
        original_scalar = session.scalar
        first_lookup = True

        def hide_existing_run(statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal first_lookup
            if first_lookup:
                first_lookup = False
                return None
            return original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", hide_existing_run)
        with pytest.raises(DomainRuleError, match="style_suite_idempotency_conflict"):
            service.create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key="race-scope-change",
                pilot_scope=StyleSuitePilotScope(
                    scene_style_version_id=replacement.version.id,
                    slot_key="detail",
                ),
            )


def test_disabled_mode_persists_previewable_matrix_and_makes_no_calls(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    class Forbidden:
        adapter_version = "forbidden"
        generation_mode = "real_local_pilot"
        publishable = False

        def generate(self, request: Any) -> Any:
            pytest.fail("disabled suite dispatched a provider")

    asset = asset_factory()
    with api_session_factory() as session:
        _historical(session, content_store)
        service = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=False),
            content_store=content_store,
            generator=Forbidden(),
        )
        result = service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-disabled",
        )
        assert result.state == "planned"
        assert result.child_job_count == 1
        assert result.candidate_count == 0
        assert result.publishable is False


def test_partial_retry_only_dispatches_the_failed_child(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    wire_calls = []
    delegate = OpenAIImageGenerator(
        _settings(budget="10"),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    wire_calls.append(request)
                    or httpx.Response(200, request=request, json=_success_payload())
                )
            )
        ),
        budget=RealImageBudget(Decimal(10)),
    )

    calls = 0
    original_generate = delegate.generate

    def fail_second_once(request: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ImageGenerationError("synthetic_second_child_failure")
        return original_generate(request)

    delegate.generate = fail_second_once  # type: ignore[method-assign]
    asset = asset_factory()
    with api_session_factory() as session:
        _historical(session, content_store, name="warm")
        _historical(session, content_store, name="white")
        card = _confirmed_identity_card(session, asset)
        service = _service(session, content_store, delegate)
        first = service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-retry",
            product_identity_card_version_id=card.id,
        )
        assert first.state == "partial_failure"
        assert (first.successful_child_count, first.failed_child_count) == (1, 1)
        successful_job = next(
            item.output_job_id for item in first.items if item.state == "succeeded"
        )
        second = service.create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-retry",
            product_identity_card_version_id=card.id,
        )
        assert second.id == first.id
        assert second.state == "awaiting_review", second
        assert calls == 3
        assert successful_job in {item.output_job_id for item in second.items}
        assert second.candidate_count == 6


def test_real_suite_without_confirmed_card_never_creates_child_or_provider_call(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A current cardless suite cannot manufacture a child for later billing."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="paid retry style")
        scope = StyleSuitePilotScope(
            scene_style_version_id=selected.version.id,
            slot_key="detail",
        )
        run_key = "real-suite-retry-without-ledger"
        failing = _real_generator([])

        def fail_before_provider(_: Any) -> Any:
            raise ImageGenerationError("pre_provider_failure")

        failing.generate = fail_before_provider  # type: ignore[method-assign]
        first = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=failing,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key=run_key,
            pilot_scope=scope,
        )
        assert first.state == "failed"
        assert first.items[0].output_job_id is None
        assert first.items[0].failure_reason == (
            "confirmed_product_identity_card_required"
        )

        with pytest.raises(
            DomainRuleError, match="confirmed_product_identity_card_required"
        ):
            StyleSuiteService(
                session,
                settings=StyleSuitePlanningSettings(real_generation_enabled=True),
                content_store=content_store,
                generator=_real_generator(provider_calls),
                real_pilot_authorization=_pilot_authorization(
                    asset, scope.scene_style_version_id, run_key
                ),
                enforce_real_pilot_boundary=True,
            ).create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key=run_key,
                pilot_scope=scope,
            )

        assert list(session.scalars(select(ImageOutputJob))) == []
    assert provider_calls == []


def test_real_paid_suite_retry_without_durable_authorization_fails_closed(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A card-valid retry still needs its original durable paid ledger row."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="paid retry style")
        card = _confirmed_identity_card(session, asset)
        observation = _confirmed_identity_observation(session, asset, card)
        observation_id = observation.id
        scope = StyleSuitePilotScope(
            scene_style_version_id=selected.version.id,
            slot_key="detail",
        )
        run_key = "real-suite-retry-without-ledger-card-valid"
        failing = _real_generator([])

        def fail_before_provider(_: Any) -> Any:
            raise ImageGenerationError("pre_provider_failure")

        failing.generate = fail_before_provider  # type: ignore[method-assign]
        first = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=failing,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key=run_key,
            pilot_scope=scope,
            product_identity_card_version_id=card.id,
            product_identity_observation_run_id=observation_id,
        )
        assert first.state == "failed"
        job_id = first.items[0].output_job_id
        assert job_id is not None

        replayed = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=_real_generator(provider_calls),
            real_pilot_authorization=_pilot_authorization(
                asset, scope.scene_style_version_id, run_key
            ),
            enforce_real_pilot_boundary=True,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key=run_key,
            pilot_scope=scope,
            product_identity_card_version_id=card.id,
            product_identity_observation_run_id=observation_id,
        )

        persisted_job = session.get(ImageOutputJob, job_id)
        assert replayed.state == "failed"
        assert replayed.items[0].failure_reason == (
            "real_image_pilot_authorization_required"
        )
        assert persisted_job is not None
        assert persisted_job.status == "generation_failed"
    assert provider_calls == []


def test_real_dispatch_evidence_survives_publish_failure_and_process_restart(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A replay after local failure must never redispatch an already paid batch."""
    from test_classification_bridge_api import _real_generator

    class FailSecondRealPublishStore(FailSecondPublishStore):
        def __getattr__(self, name: str) -> Any:
            return getattr(self._delegate, name)

    provider_calls = []
    asset = asset_factory()
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="warm")
        card = _confirmed_identity_card(session, asset)
        observation = _confirmed_identity_observation(session, asset, card)
        observation_id = observation.id
        card_id = card.id
        scope = StyleSuitePilotScope(
            scene_style_version_id=selected.version.id,
            slot_key="detail",
        )
        first = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=FailSecondRealPublishStore(content_store),
            generator=_real_generator(provider_calls),
            allow_synthetic_product_style=True,
            real_pilot_authorization=_pilot_authorization(
                asset, scope.scene_style_version_id, "restart-after-provider-success"
            ),
            enforce_real_pilot_boundary=True,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="restart-after-provider-success",
            pilot_scope=scope,
            product_identity_card_version_id=card_id,
            product_identity_observation_run_id=observation_id,
        )
        assert first.state == "failed"
        assert len(provider_calls) == 1
        job_id = first.items[0].output_job_id
        assert job_id is not None
        assert "test-only-key" not in json.dumps(
            [event.payload for event in session.scalars(select(AuditEvent))]
        )
        attempts = [
            event.payload["outbound_attempts"]
            for event in session.scalars(select(AuditEvent))
            if event.payload.get("job_id") == str(job_id)
            and event.event_type == "image_output.provider_dispatch_succeeded"
        ]
        assert attempts == [1]

    with api_session_factory() as fresh_session:
        replay = StyleSuiteService(
            fresh_session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=_real_generator(provider_calls),
            allow_synthetic_product_style=True,
            real_pilot_authorization=_pilot_authorization(
                asset, scope.scene_style_version_id, "restart-after-provider-success"
            ),
            enforce_real_pilot_boundary=True,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="restart-after-provider-success",
            pilot_scope=scope,
            product_identity_card_version_id=card_id,
            product_identity_observation_run_id=observation_id,
        )

        assert replay.id == first.id
        assert replay.state == "needs_reconciliation"
        assert replay.candidate_count == 0
        attempts = [
            event.payload["outbound_attempts"]
            for event in fresh_session.scalars(select(AuditEvent))
            if event.payload.get("job_id") == str(job_id)
            and event.event_type == "image_output.provider_dispatch_succeeded"
        ]
        assert attempts == [1]
    assert len(provider_calls) == 1


def test_pre_dispatch_reservation_crash_replays_as_uncertain_without_provider_call(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A restart must treat the committed pre-dispatch fence as unknowable spend."""
    from test_classification_bridge_api import _real_generator

    provider_calls = []
    asset = asset_factory()
    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="warm")
        card = _confirmed_identity_card(session, asset)
        observation = _confirmed_identity_observation(session, asset, card)
        observation_id = observation.id
        card_id = card.id
        scope = StyleSuitePilotScope(
            scene_style_version_id=selected.version.id,
            slot_key="detail",
        )
        crashing = _real_generator(provider_calls)

        def die_after_reservation(_: Any) -> Any:
            raise SystemExit("simulated process death")

        crashing.generate = die_after_reservation  # type: ignore[method-assign]
        with pytest.raises(SystemExit, match="simulated process death"):
            StyleSuiteService(
                session,
                settings=StyleSuitePlanningSettings(real_generation_enabled=True),
                content_store=content_store,
                generator=crashing,
                allow_synthetic_product_style=True,
                real_pilot_authorization=_pilot_authorization(
                    asset, scope.scene_style_version_id, "crash-after-pre-dispatch"
                ),
                enforce_real_pilot_boundary=True,
            ).create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key="crash-after-pre-dispatch",
                pilot_scope=scope,
                product_identity_card_version_id=card_id,
                product_identity_observation_run_id=observation_id,
            )
        assert provider_calls == []
        events = list(session.scalars(select(AuditEvent)))
        dispatch = next(
            event
            for event in events
            if event.event_type == "image_output.provider_dispatch_uncertain"
        )
        assert dispatch.payload["outbound_attempts"] == 0
        assert "test-only-key" not in json.dumps(dispatch.payload)
        assert asset.content_sha256 not in json.dumps(dispatch.payload)

    with api_session_factory() as fresh_session:
        replay = StyleSuiteService(
            fresh_session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=_real_generator(provider_calls),
            allow_synthetic_product_style=True,
            real_pilot_authorization=_pilot_authorization(
                asset, scope.scene_style_version_id, "crash-after-pre-dispatch"
            ),
            enforce_real_pilot_boundary=True,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="crash-after-pre-dispatch",
            pilot_scope=scope,
            product_identity_card_version_id=card_id,
            product_identity_observation_run_id=observation_id,
        )
    assert replay.state == "needs_reconciliation"
    assert len(provider_calls) == 0


def test_remote_protocol_failure_replay_uses_durable_dispatch_fence(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    """A socket-level unknown outcome cannot be retried by a fresh process."""
    from test_classification_bridge_api import _real_generator

    provider_calls = []
    asset = asset_factory()

    def protocol_failure(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        raise httpx.RemoteProtocolError("response lost after request write")

    with api_session_factory() as session:
        selected, _ = _historical(session, content_store, name="warm")
        card = _confirmed_identity_card(session, asset)
        observation = _confirmed_identity_observation(session, asset, card)
        observation_id = observation.id
        card_id = card.id
        scope = StyleSuitePilotScope(
            scene_style_version_id=selected.version.id, slot_key="detail"
        )
        generator = OpenAIImageGenerator(
            _settings(),
            transport=httpx.Client(transport=httpx.MockTransport(protocol_failure)),
            budget=RealImageBudget(Decimal("1.00")),
        )
        first = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=generator,
            allow_synthetic_product_style=True,
            real_pilot_authorization=_pilot_authorization(
                asset, scope.scene_style_version_id, "remote-protocol-replay"
            ),
            enforce_real_pilot_boundary=True,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="remote-protocol-replay",
            pilot_scope=scope,
            product_identity_card_version_id=card_id,
            product_identity_observation_run_id=observation_id,
        )
        assert first.state == "failed"
        assert len(provider_calls) == 1

    with api_session_factory() as fresh_session:
        replay = StyleSuiteService(
            fresh_session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=_real_generator(provider_calls),
            allow_synthetic_product_style=True,
            real_pilot_authorization=_pilot_authorization(
                asset, scope.scene_style_version_id, "remote-protocol-replay"
            ),
            enforce_real_pilot_boundary=True,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="remote-protocol-replay",
            pilot_scope=scope,
            product_identity_card_version_id=card_id,
            product_identity_observation_run_id=observation_id,
        )
    assert replay.state == "needs_reconciliation"
    assert len(provider_calls) == 1


def test_get_and_parent_key_are_customer_scoped(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        _historical(session, content_store)
        service = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=False),
        )
        run = service.create(
            CUSTOMER_A_ID, asset.product_id, asset.id, idempotency_key="owned"
        )
        with pytest.raises(DomainRuleError, match="style_suite_run_not_found"):
            service.get(CUSTOMER_B_ID, run.id)
        with pytest.raises(DomainRuleError, match="style_suite_idempotency_conflict"):
            service.create(CUSTOMER_A_ID, "different", uuid4(), idempotency_key="owned")


def test_uncertain_child_never_auto_dispatches_again_across_sessions(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_classification_bridge_api import _real_generator

    calls = 0

    def uncertain(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise ImageGenerationOutcomeUncertainError

    monkeypatch.setattr(ImageJobService, "create", uncertain)
    asset = asset_factory()
    provider_calls = []
    generator = _real_generator(provider_calls)
    with api_session_factory() as session:
        _historical(session, content_store)
        first = _service(session, content_store, generator).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-uncertain",
        )
        run_id = first.id
        assert first.state == "needs_reconciliation"
        assert first.uncertain_child_count == 1
    with api_session_factory() as fresh_session:
        replay = _service(fresh_session, content_store, generator).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="suite-uncertain",
        )
        assert replay.id == run_id
        assert replay.state == "needs_reconciliation"
    assert calls == 1
    assert provider_calls == []


def test_success_plus_pending_aggregates_as_running(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        _historical(session, content_store, name="warm")
        _historical(session, content_store, name="white")
        service = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=False),
        )
        run = service.create(
            CUSTOMER_A_ID, asset.product_id, asset.id, idempotency_key="suite-running"
        )
        first_item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        )
        assert first_item is not None
        first_item.state = "succeeded"
        session.commit()
        current = service.get(CUSTOMER_A_ID, run.id)
        assert current.state == "running"
        assert (current.successful_child_count, current.pending_child_count) == (1, 1)


def test_child_job_binding_rejects_wrong_job_cross_customer_and_rebinding(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    from test_classification_bridge_api import _real_generator
    from test_style_suite_planning import confirm

    asset = asset_factory()
    with api_session_factory() as session:
        _historical(session, content_store, name="binding-one")
        _historical(session, content_store, name="binding-two")
        card = _confirmed_identity_card(session, asset)
        completed = _service(session, content_store, _real_generator([])).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="bound-suite",
            product_identity_card_version_id=card.id,
        )
        bound_items = list(
            session.scalars(
                select(StyleSuiteRunItem).where(
                    StyleSuiteRunItem.run_id == completed.id
                )
            )
        )
        original_job_id = bound_items[0].output_job_id
        assert original_job_id is not None

        planned = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=False),
        ).create(
            CUSTOMER_A_ID, asset.product_id, asset.id, idempotency_key="other-suite"
        )
        wrong_target = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == planned.id)
        )
        assert wrong_target is not None
        wrong_target.output_job_id = original_job_id
        with pytest.raises(DomainRuleError, match="style_suite_item_job_mismatch"):
            session.commit()
        session.rollback()

        second_job_id = bound_items[-1].output_job_id
        assert second_job_id is not None
        rebound = session.get(StyleSuiteRunItem, bound_items[0].id)
        assert rebound is not None
        rebound.output_job_id = None
        with pytest.raises(DomainRuleError, match="style_suite_item_job_immutable"):
            session.commit()
        session.rollback()
        rebound = session.get(StyleSuiteRunItem, bound_items[0].id)
        assert rebound is not None
        rebound.output_job_id = second_job_id
        with pytest.raises(DomainRuleError, match="style_suite_item_job_immutable"):
            session.commit()
        session.rollback()

    foreign_asset = asset_factory(customer_id=CUSTOMER_B_ID, product_id="foreign")
    with api_session_factory() as session:
        confirm(session, "foreign-style", ["detail"], owner=CUSTOMER_B_ID)
        foreign_run = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=False),
        ).create(
            CUSTOMER_B_ID,
            foreign_asset.product_id,
            foreign_asset.id,
            idempotency_key="foreign-suite",
        )
        foreign_item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == foreign_run.id)
        )
        assert foreign_item is not None
        foreign_item.output_job_id = original_job_id
        with pytest.raises(DomainRuleError, match="style_suite_item_job_mismatch"):
            session.commit()


def test_0020_upgrade_backfills_multiple_legacy_runs_uniquely(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-suite.db'}"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0019_style_suite_runs")
    engine = create_database_engine(database_url)
    now = "2026-09-08T00:00:00+00:00"
    customer = str(CUSTOMER_A_ID)
    asset_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:customer, 'legacy', :now)"
            ),
            {"customer": customer, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO product_assets "
                "(id, customer_id, product_id, photo_id, identity, confirmed, source_type, "
                "quality_flags, occurred_at, content_sha256, mime_type, width, height, created_at) "
                "VALUES (:id, :customer, 'legacy-product', 'legacy-photo', "
                "'original_real_photo', 1, 'synthetic_metadata', '[]', :now, :sha, "
                "'image/jpeg', 10, 10, :now)"
            ),
            {"id": asset_id, "customer": customer, "now": now, "sha": "a" * 64},
        )
        for run_id in (str(uuid4()), str(uuid4())):
            connection.execute(
                text(
                    "INSERT INTO style_suite_runs "
                    "(id, customer_id, product_id, source_asset_id, source_content_sha256, "
                    "style_count, slot_count, child_job_count, candidate_count, "
                    "candidate_estimate_usd, maximum_estimated_cost_usd, cost_basis, currency, "
                    "real_generation_enabled, state, created_at) VALUES "
                    "(:id, :customer, 'legacy-product', :asset, :sha, 1, 1, 1, 3, "
                    "0.1, 0.3, 'local_planning_estimate', 'USD', 0, 'planned', :now)"
                ),
                {
                    "id": run_id,
                    "customer": customer,
                    "asset": asset_id,
                    "sha": "a" * 64,
                    "now": now,
                },
            )
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, idempotency_key FROM style_suite_runs ORDER BY id")
        ).all()
    engine.dispose()
    assert len(rows) == 2
    assert {key for _, key in rows} == {f"legacy:{run_id}" for run_id, _ in rows}
