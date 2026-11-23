"""Cardinality contract for one-product style generation."""
# ruff: noqa: F811

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, update
from test_image_jobs_api import (  # noqa: F401
    CUSTOMER_A_ID,
    api_session_factory,
    asset_factory,
    content_store,
    style_factory,
)
from test_openai_image_provider import _jpeg, _request, _settings, _success_payload
from test_real_product_identity_authorization import identity_payload
from test_style_template_slots import confirmation, group

from opc_style.api.routes_image_jobs import build_image_generator
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import (
    DeterministicLocalImageGenerator,
    ImageGenerationError,
)
from opc_style.domain.output_rules import validate_product_count
from opc_style.domain.product_identity import (
    identity_card_payload,
    identity_card_sha256,
    normalize_identity_draft,
)
from opc_style.models import (
    AssetStoreRecoveryBinding,
    AuditEvent,
    ImageOutputJob,
    ProductAssetLink,
    ProductIdentityAnalysisRun,
    ProductIdentityCardVersion,
    ProductIdentityObservationRun,
    RealImagePilotAuthorization,
    SceneStyle,
    StyleCardVersion,
    StyleSample,
)
from opc_style.providers.openai_images import OpenAIImageGenerator, RealImageBudget
from opc_style.schemas import HistoricalStyleGroupsConfirmRequest, StyleSuitePilotScope
from opc_style.services.image_jobs import (
    ImageGenerationOutcomeUncertainError,
    ImageJobService,
    ImageSourceSelection,
)
from opc_style.services.real_pilot_authorization import (
    issue_real_pilot_authorization,
)
from opc_style.services.samples import StyleSampleService
from opc_style.services.style_suites import StyleSuiteService
from opc_style.services.styles import HistoricalStyleConfirmationService
from opc_style.settings import RealImageProviderSettings, StyleSuitePlanningSettings


def _confirmed_identity_card(session: Any, asset: Any) -> ProductIdentityCardVersion:
    draft = normalize_identity_draft(identity_payload())
    payload = identity_card_payload(draft)
    analysis = ProductIdentityAnalysisRun(
        customer_id=asset.customer_id,
        product_asset_id=asset.id,
        product_id=asset.product_id,
        source_content_sha256=asset.content_sha256,
        idempotency_key=f"identity-analysis-{uuid4()}",
        model="recording-product-identity-v1",
        authorized_usd=Decimal("0.10"),
        input_manifest_sha256="a" * 64,
        input_manifest={"input_image_count": 1},
        dispatch_state="provider_responded",
        outbound_attempts=1,
        proposal=payload,
    )
    session.add(analysis)
    session.flush()
    card = ProductIdentityCardVersion(
        customer_id=asset.customer_id,
        product_asset_id=asset.id,
        product_id=asset.product_id,
        analysis_run_id=analysis.id,
        version=1,
        source_content_sha256=asset.content_sha256,
        card_payload=payload,
        card_sha256=identity_card_sha256(draft),
        confirmed_by="local_customer",
    )
    session.add(card)
    session.commit()
    return card


def _confirmed_identity_observation(
    session: Any,
    asset: Any,
    card: ProductIdentityCardVersion,
    *,
    decision: str = "pass",
    reason_codes: list[str] | None = None,
) -> ProductIdentityObservationRun:
    """Persist one internally consistent passing observation fixture."""
    payload = card.card_payload
    reasons = [] if reason_codes is None else reason_codes
    result = {
        "observation": payload,
        "decision": decision,
        "reason_codes": reasons,
        "policy_version": "product-identity-gate-v1",
    }
    result_sha256 = sha256(
        json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    observation = ProductIdentityObservationRun(
        customer_id=asset.customer_id,
        product_asset_id=asset.id,
        product_id=asset.product_id,
        source_content_sha256=asset.content_sha256,
        product_identity_card_version_id=card.id,
        product_identity_card_sha256=card.card_sha256,
        idempotency_key=f"identity-observation-{uuid4()}",
        observer="recording-product-identity-v1",
        observer_version="recording-product-identity-v1",
        observation_mode="real_product_identity_proposal",
        policy_version="product-identity-gate-v1",
        authorized_usd=Decimal("0.10"),
        input_manifest_sha256="b" * 64,
        input_manifest={"input_image_count": 1},
        dispatch_state="provider_responded",
        outbound_attempts=1,
        observation_payload=payload,
        decision=decision,
        reason_codes=reasons,
        result_sha256=result_sha256,
    )
    session.add(observation)
    session.commit()
    return observation


def _identity_card_facts(card: ProductIdentityCardVersion) -> dict[str, Any]:
    return {
        "product_identity_card_version_id": card.id,
        "product_identity_card_sha256": card.card_sha256,
        "product_identity_card": card.card_payload,
    }


def _rewrite_job_as_preexisting_cardless_fixture(
    session: Any, job: ImageOutputJob, *, cohort_marker: str | None
) -> ImageOutputJob:
    """Model a persisted pre-fix row without adding a production bypass."""
    metadata = dict(job.request_metadata)
    for key in (
        "product_identity_card_version_id",
        "product_identity_card_sha256",
        "product_identity_card",
    ):
        metadata.pop(key, None)
    if cohort_marker is None:
        metadata.pop("product_identity_card_requirement", None)
    else:
        metadata["product_identity_card_requirement"] = cohort_marker
    session.execute(
        update(ImageOutputJob)
        .where(ImageOutputJob.id == job.id)
        .values(request_metadata=metadata)
    )
    session.commit()
    session.expire_all()
    persisted = session.get(ImageOutputJob, job.id)
    assert persisted is not None
    return persisted


def test_product_style_requires_exactly_one_product() -> None:
    assert validate_product_count("product_style", ["product-1"]) == ("product-1",)

    for invalid in ([], ["product-1", "product-2"]):
        with pytest.raises(DomainRuleError, match="product_style_requires_one_product"):
            validate_product_count("product_style", invalid)


def test_source_role_service_rejects_two_identity_images_before_job_creation(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    style_factory: Any,
) -> None:
    from opc_style.models import ImageOutputJob

    _, scene, version = style_factory()
    assets = [asset_factory(), asset_factory()]
    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_success_payload())
            )
        ),
        budget=RealImageBudget(Decimal(1)),
    )
    with api_session_factory() as session:
        with pytest.raises(DomainRuleError, match="generation_source_identity_count"):
            ImageJobService(session, content_store, generator).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=tuple(
                    ImageSourceSelection(asset.product_id, asset.id) for asset in assets
                ),
                scene_style_id=scene.id,
                style_card_version_id=version.id,
                output_ratio="1:1",
                idempotency_key="two-identities",
            )
        assert list(session.scalars(select(ImageOutputJob))) == []


def test_runtime_builder_keeps_real_provider_disabled_by_default() -> None:
    generator = build_image_generator(RealImageProviderSettings(_env_file=None))

    assert type(generator) is DeterministicLocalImageGenerator


def test_runtime_builder_uses_exact_real_provider_when_explicitly_enabled() -> None:
    generator = build_image_generator(
        RealImageProviderSettings(
            enabled=True,
            api_key="test-secret-value",
            max_run_usd="1.00",
            _env_file=None,
        )
    )

    assert type(generator) is OpenAIImageGenerator
    assert generator.publishable is False


def _historical(
    session: Any,
    store: Any,
    count: int = 10,
    name: str = "warm history",
    slot_keys: tuple[str, ...] = ("detail",),
    slot_props: list[str] | None = None,
    slot_camera_angle: str | None = None,
) -> Any:
    ids = []
    for index in range(count):
        content = _jpeg((index, 30, 40))
        sample = StyleSample(
            customer_id=CUSTOMER_A_ID,
            photo_id=str(uuid4()),
            authorized_at=datetime.now(UTC),
            consent_provenance="synthetic-test",
            consent_text_version="v1",
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/jpeg",
            width=32,
            height=32,
            asset_store_namespace=store.store_namespace,
        )
        session.add(sample)
        session.flush()
        store.register_style_sample_content(
            CUSTOMER_A_ID, sample.id, content, sample.content_sha256
        )
        ids.append(str(sample.id))
    session.commit()
    group_facts = group(name, ids, list(slot_keys))
    if slot_props is not None:
        for slot_facts in group_facts["slots"]:
            slot_facts["rules"]["props"] = slot_props
    if slot_camera_angle is not None:
        for slot_facts in group_facts["slots"]:
            slot_facts["rules"]["camera_angle"] = slot_camera_angle
    if count >= 10:
        group_facts.update(
            analysis_mode="real_visual_analysis_proposal",
            analyzer_version="test-real-analysis-v1",
            proposal_fingerprint="f" * 64,
            palette=["warm neutral"],
            lighting=["soft diffuse"],
            composition=["centered with breathing room"],
            backgrounds=["warm seamless"],
            field_evidence={
                field: ids
                for field in (
                    "tone",
                    "palette",
                    "lighting",
                    "composition",
                    "backgrounds",
                )
            },
        )
    result = HistoricalStyleConfirmationService(session).confirm(
        CUSTOMER_A_ID,
        HistoricalStyleGroupsConfirmRequest.model_validate(
            confirmation([group_facts])
        ),
    )[0]
    return result, sorted(ids)


@pytest.mark.parametrize("legacy_violation", ["demo_origin", "synthetic_evidence"])
def test_deep_paid_boundary_rejects_legacy_demo_style_or_synthetic_evidence(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    legacy_violation: str,
) -> None:
    """A route capability cannot bless polluted style evidence at image dispatch."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, request=request, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("1.00")),
    )
    asset = asset_factory()
    with api_session_factory() as session:
        style, sample_ids = _historical(session, content_store)
        card = _confirmed_identity_card(session, asset)
        if legacy_violation == "demo_origin":
            version = session.get(StyleCardVersion, style.version.id)
            assert version is not None
            session.execute(
                update(StyleCardVersion)
                .where(StyleCardVersion.id == version.id)
                .values(content={**version.content, "style_origin": "local_demo"})
            )
        else:
            session.execute(
                update(StyleSample)
                .where(StyleSample.id.in_([UUID(value) for value in sample_ids]))
                .values(consent_provenance="local_synthetic_acceptance")
            )
        session.commit()
        capability = issue_real_pilot_authorization(
            customer_id=CUSTOMER_A_ID,
            product_id=asset.product_id,
            source_asset_id=asset.id,
            scene_style_version_id=style.version.id,
            slot_key="detail",
            idempotency_key=f"deep-paid-{legacy_violation}",
            authorized_usd=Decimal("1.00"),
        )

        with pytest.raises(
            DomainRuleError,
            match="real_pilot_customer_upload_style_required",
        ):
            ImageJobService(
                session,
                content_store,
                generator,
                real_pilot_authorization=capability,
                enforce_real_pilot_boundary=True,
            ).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key=f"deep-paid-{legacy_violation}",
                **_identity_card_facts(card),
            )

    assert calls == []


def test_deep_paid_boundary_rejects_capability_reuse_for_a_new_run_key(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, request=request, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("1.00")),
    )
    asset = asset_factory()
    with api_session_factory() as session:
        style, _ = _historical(session, content_store)
        card = _confirmed_identity_card(session, asset)
        capability = issue_real_pilot_authorization(
            customer_id=CUSTOMER_A_ID,
            product_id=asset.product_id,
            source_asset_id=asset.id,
            scene_style_version_id=style.version.id,
            slot_key="detail",
            idempotency_key="authorized-run-key",
            authorized_usd=Decimal("1.00"),
        )

        with pytest.raises(
            DomainRuleError, match="real_image_pilot_authorization_required"
        ):
            ImageJobService(
                session,
                content_store,
                generator,
                real_pilot_authorization=capability,
                enforce_real_pilot_boundary=True,
            ).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key="different-run-key",
                **_identity_card_facts(card),
            )

    assert calls == []


@pytest.mark.parametrize("card_case", ["missing", "foreign_asset"])
def test_deep_paid_create_requires_current_confirmed_identity_before_authorization(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    card_case: str,
) -> None:
    provider_calls: list[httpx.Request] = []
    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    provider_calls.append(request)
                    or httpx.Response(200, request=request, json=_success_payload())
                )
            )
        ),
        budget=RealImageBudget(Decimal("1.00")),
    )
    asset = asset_factory()
    foreign_asset = asset_factory()

    class RecordingCapability:
        authorized_usd = Decimal("1.00")
        calls = 0

        def authorizes_image(self, **facts: Any) -> bool:
            del facts
            self.calls += 1
            return True

    with api_session_factory() as session:
        style, _ = _historical(session, content_store)
        foreign_card = _confirmed_identity_card(session, foreign_asset)
        capability = RecordingCapability()
        card_facts = (
            {}
            if card_case == "missing"
            else {
                "product_identity_card_version_id": foreign_card.id,
                "product_identity_card_sha256": foreign_card.card_sha256,
                "product_identity_card": foreign_card.card_payload,
            }
        )

        with pytest.raises(
            DomainRuleError,
            match=(
                "confirmed_product_identity_card_required"
                if card_case == "missing"
                else "product_identity_card_scope_mismatch"
            ),
        ):
            ImageJobService(
                session,
                content_store,
                generator,
                real_pilot_authorization=capability,  # type: ignore[arg-type]
                enforce_real_pilot_boundary=True,
            ).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key=f"deep-card-{card_case}",
                **card_facts,  # type: ignore[arg-type]
            )

        assert capability.calls == 0
        assert list(session.scalars(select(ImageOutputJob))) == []
    assert provider_calls == []


def test_default_real_product_style_without_card_stops_before_generator_or_http(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    """The default constructor cannot disable the real product identity gate."""
    http_calls: list[httpx.Request] = []
    generator_calls: list[Any] = []
    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: http_calls.append(request)
                or httpx.Response(200, request=request, json=_success_payload())
            )
        ),
        budget=RealImageBudget(Decimal("1.00")),
    )
    generate = generator.generate

    def record_generate(request: Any) -> Any:
        generator_calls.append(request)
        return generate(request)

    generator.generate = record_generate  # type: ignore[method-assign]
    asset = asset_factory()
    with api_session_factory() as session:
        style, _ = _historical(session, content_store)

        with pytest.raises(
            DomainRuleError, match="confirmed_product_identity_card_required"
        ):
            ImageJobService(session, content_store, generator).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key="default-real-missing-card",
            )

        assert list(session.scalars(select(ImageOutputJob))) == []
    assert generator_calls == []
    assert http_calls == []


def _pending_real_paid_replay_job(
    session: Any,
    content_store: Any,
    asset: Any,
    *,
    status: str,
) -> tuple[Any, ImageOutputJob, list[str], Any, ProductIdentityCardVersion]:
    """Stop a fully authorized real job before its first dispatch claim."""
    from test_classification_bridge_api import _real_generator

    style, sample_ids = _historical(session, content_store)
    card = _confirmed_identity_card(session, asset)
    run_key = f"paid-{status}-replay"
    capability = issue_real_pilot_authorization(
        customer_id=CUSTOMER_A_ID,
        product_id=asset.product_id,
        source_asset_id=asset.id,
        scene_style_version_id=style.version.id,
        slot_key="detail",
        idempotency_key=run_key,
        authorized_usd=Decimal("1.00"),
    )
    service = ImageJobService(
        session,
        content_store,
        _real_generator([]),
        real_pilot_authorization=capability,
        enforce_real_pilot_boundary=True,
    )

    def stop_before_claim(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise SystemExit("stop before paid claim")

    service._execute_or_replay = stop_before_claim  # type: ignore[method-assign]
    with pytest.raises(SystemExit, match="stop before paid claim"):
        service.create(
            CUSTOMER_A_ID,
            output_type="product_style",
            sources=(ImageSourceSelection(asset.product_id, asset.id),),
            scene_style_id=style.scene.id,
            style_card_version_id=style.version.id,
            output_ratio="4:5",
            slot_key="detail",
            idempotency_key=run_key,
            **_identity_card_facts(card),
        )
    job = session.scalar(
        select(ImageOutputJob).where(ImageOutputJob.idempotency_key == run_key)
    )
    assert job is not None
    authorization = session.scalar(
        select(RealImagePilotAuthorization).where(
            RealImagePilotAuthorization.output_job_id == job.id
        )
    )
    assert authorization is not None
    assert authorization.dispatch_state == "reserved"
    assert authorization.outbound_attempts == 0
    if status == "generating":
        job.status = "generating"
        job.generation_claim_token = "11111111-1111-4111-8111-111111111111"
        job.generation_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        session.commit()
    return style, job, sample_ids, capability, card


def _set_real_dispatch_evidence(
    session: Any, job: ImageOutputJob, evidence: str
) -> RealImagePilotAuthorization:
    authorization = session.scalar(
        select(RealImagePilotAuthorization).where(
            RealImagePilotAuthorization.output_job_id == job.id
        )
    )
    if authorization is None:
        authorization = RealImagePilotAuthorization(
            customer_id=CUSTOMER_A_ID,
            output_job_id=job.id,
            budget_scope=f"real_image_pilot:{job.id}",
            authorized_usd=Decimal("1.00"),
        )
        session.add(authorization)
    if evidence in {"dispatch_uncertain", "provider_responded"}:
        authorization.dispatch_state = evidence
    elif evidence == "authorization_attempts":
        authorization.outbound_attempts = 1
    elif evidence == "audit_attempts":
        session.add(
            AuditEvent(
                customer_id=CUSTOMER_A_ID,
                event_type="image_output.provider_dispatch_succeeded",
                payload={"job_id": str(job.id), "outbound_attempts": 1},
            )
        )
    else:  # pragma: no cover - the parametrization owns this closed vocabulary.
        raise AssertionError(f"unsupported evidence: {evidence}")
    session.commit()
    return authorization


def _real_dispatch_state_snapshot(
    session: Any, job: ImageOutputJob, authorization: RealImagePilotAuthorization
) -> tuple[tuple[Any, ...], tuple[Any, ...], tuple[tuple[Any, ...], ...]]:
    job_snapshot = (
        job.status,
        job.failure_reason,
        job.generation_claim_token,
        job.generation_lease_expires_at,
        json.dumps(job.request_metadata, sort_keys=True),
    )
    ledger_snapshot = (
        authorization.id,
        authorization.customer_id,
        authorization.output_job_id,
        authorization.budget_scope,
        authorization.authorized_usd,
        authorization.dispatch_state,
        authorization.outbound_attempts,
    )
    audit_snapshot = tuple(
        (
            event.id,
            event.event_type,
            json.dumps(event.payload, sort_keys=True),
        )
        for event in session.scalars(
            select(AuditEvent)
            .where(AuditEvent.customer_id == CUSTOMER_A_ID)
            .order_by(AuditEvent.id)
        )
    )
    return job_snapshot, ledger_snapshot, audit_snapshot


@pytest.mark.parametrize("status", ["queued", "generating"])
@pytest.mark.parametrize(
    "evidence",
    [
        "dispatch_uncertain",
        "provider_responded",
        "authorization_attempts",
        "audit_attempts",
    ],
)
def test_non_enforced_paid_pending_replay_rejects_prior_dispatch_evidence_before_mutation(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    evidence: str,
) -> None:
    """A direct real-service replay must never bypass durable no-repeat evidence."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    publisher_calls: list[tuple[Any, ...]] = []
    with api_session_factory() as session:
        style, job, _, _, card = _pending_real_paid_replay_job(
            session, content_store, asset, status=status
        )
        authorization = _set_real_dispatch_evidence(session, job, evidence)
        session.expire_all()
        job = session.get(ImageOutputJob, job.id)
        authorization = session.get(RealImagePilotAuthorization, authorization.id)
        assert job is not None and authorization is not None
        before = _real_dispatch_state_snapshot(session, job, authorization)
        original_reserve = content_store.reserve_derivative

        def track_publish(*args: Any, **kwargs: Any) -> Any:
            publisher_calls.append(args)
            return original_reserve(*args, **kwargs)

        monkeypatch.setattr(content_store, "reserve_derivative", track_publish)
        service = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            enforce_real_pilot_boundary=False,
        )

        with pytest.raises(ImageGenerationOutcomeUncertainError):
            service.create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key=f"paid-{status}-replay",
                **_identity_card_facts(card),
            )

        session.expire_all()
        persisted = session.get(ImageOutputJob, job.id)
        persisted_authorization = session.get(
            RealImagePilotAuthorization, authorization.id
        )
        assert persisted is not None and persisted_authorization is not None
        assert (
            _real_dispatch_state_snapshot(session, persisted, persisted_authorization)
            == before
        )
    assert provider_calls == []
    assert publisher_calls == []


@pytest.mark.parametrize("status", ["queued", "generating"])
@pytest.mark.parametrize(
    "invalid_paid_state",
    [
        "local_demo_origin",
        "synthetic_evidence",
        "dispatch_uncertain",
        "provider_responded",
        "positive_attempts",
    ],
)
def test_paid_pending_replay_rejects_before_mutating_job_or_ledger(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    invalid_paid_state: str,
) -> None:
    """Queued and generating replays must pass the same deepest paid gate."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    publisher_calls: list[tuple[Any, ...]] = []
    with api_session_factory() as session:
        style, job, sample_ids, capability, card = _pending_real_paid_replay_job(
            session, content_store, asset, status=status
        )
        authorization = session.scalar(
            select(RealImagePilotAuthorization).where(
                RealImagePilotAuthorization.output_job_id == job.id
            )
        )
        assert authorization is not None
        if invalid_paid_state == "local_demo_origin":
            version = session.get(StyleCardVersion, style.version.id)
            assert version is not None
            local_demo_content = {**version.content, "style_origin": "local_demo"}
            session.execute(
                update(SceneStyle)
                .where(SceneStyle.id == style.scene.id)
                .values(style_origin="local_demo")
            )
            session.execute(
                update(StyleCardVersion)
                .where(StyleCardVersion.id == version.id)
                .values(content=local_demo_content)
            )
            session.execute(
                update(ImageOutputJob)
                .where(ImageOutputJob.id == job.id)
                .values(
                    request_metadata={
                        **job.request_metadata,
                        "style_snapshot": local_demo_content,
                    }
                )
            )
        elif invalid_paid_state == "synthetic_evidence":
            session.execute(
                update(StyleSample)
                .where(StyleSample.id.in_([UUID(value) for value in sample_ids]))
                .values(consent_provenance="local_synthetic_acceptance")
            )
        elif invalid_paid_state in {"dispatch_uncertain", "provider_responded"}:
            authorization.dispatch_state = invalid_paid_state
        elif invalid_paid_state == "positive_attempts":
            authorization.outbound_attempts = 1
        session.commit()
        session.expire_all()
        job = session.get(ImageOutputJob, job.id)
        authorization = session.scalar(
            select(RealImagePilotAuthorization).where(
                RealImagePilotAuthorization.output_job_id == job.id
            )
        )
        assert job is not None and authorization is not None
        job_before = (
            job.status,
            job.generation_claim_token,
            job.generation_lease_expires_at,
        )
        ledger_before = (
            authorization.dispatch_state,
            authorization.outbound_attempts,
        )
        audit_before = [
            (event.id, event.event_type, event.payload)
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.customer_id == CUSTOMER_A_ID)
            )
        ]
        original_reserve = content_store.reserve_derivative

        def track_publish(*args: Any, **kwargs: Any) -> Any:
            publisher_calls.append(args)
            return original_reserve(*args, **kwargs)

        monkeypatch.setattr(content_store, "reserve_derivative", track_publish)
        service = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            real_pilot_authorization=capability,
            enforce_real_pilot_boundary=True,
        )
        expected_error = (
            ImageGenerationOutcomeUncertainError
            if invalid_paid_state
            in {"dispatch_uncertain", "provider_responded", "positive_attempts"}
            else DomainRuleError
        )
        with pytest.raises(expected_error):
            service.create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key=f"paid-{status}-replay",
                **_identity_card_facts(card),
            )

        session.expire_all()
        persisted = session.get(ImageOutputJob, job.id)
        persisted_authorization = session.scalar(
            select(RealImagePilotAuthorization).where(
                RealImagePilotAuthorization.output_job_id == job.id
            )
        )
        audit_after = [
            (event.id, event.event_type, event.payload)
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.customer_id == CUSTOMER_A_ID)
            )
        ]
        assert persisted is not None and persisted_authorization is not None
        assert (
            persisted.status,
            persisted.generation_claim_token,
            persisted.generation_lease_expires_at,
        ) == job_before
        assert (
            persisted_authorization.dispatch_state,
            persisted_authorization.outbound_attempts,
        ) == ledger_before
        assert audit_after == audit_before
    assert provider_calls == []
    assert publisher_calls == []


@pytest.mark.parametrize("status", ["queued", "generating"])
def test_paid_pending_replay_dispatches_once_when_reserved_and_customer_uploaded(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    status: str,
) -> None:
    """One fresh capability may resume one unused customer-upload job."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    with api_session_factory() as session:
        style, job, _, capability, card = _pending_real_paid_replay_job(
            session, content_store, asset, status=status
        )
        result = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            real_pilot_authorization=capability,
            enforce_real_pilot_boundary=True,
        ).create(
            CUSTOMER_A_ID,
            output_type="product_style",
            sources=(ImageSourceSelection(asset.product_id, asset.id),),
            scene_style_id=style.scene.id,
            style_card_version_id=style.version.id,
            output_ratio="4:5",
            slot_key="detail",
            idempotency_key=f"paid-{status}-replay",
            **_identity_card_facts(card),
        )
        authorization = session.scalar(
            select(RealImagePilotAuthorization).where(
                RealImagePilotAuthorization.output_job_id == job.id
            )
        )
        reservations = [
            event
            for event in session.scalars(select(AuditEvent))
            if event.event_type == "image_output.provider_dispatch_uncertain"
            and event.payload.get("job_id") == str(job.id)
        ]
        assert result.job.status == "machine_check"
        assert len(provider_calls) == 1
        assert len(reservations) == 1
        assert authorization is not None
        assert authorization.dispatch_state == "provider_responded"
        assert authorization.outbound_attempts == 1


def test_markerless_migration_pending_job_retains_legacy_paid_replay_compatibility(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    """A pre-marker queued row may resume without fabricating an identity card."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    with api_session_factory() as session:
        style, job, _, capability, _ = _pending_real_paid_replay_job(
            session, content_store, asset, status="queued"
        )
        job = _rewrite_job_as_preexisting_cardless_fixture(
            session, job, cohort_marker=None
        )
        run_key = "paid-queued-replay"

        result = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            real_pilot_authorization=capability,
            enforce_real_pilot_boundary=True,
        ).create(
            CUSTOMER_A_ID,
            output_type="product_style",
            sources=(ImageSourceSelection(asset.product_id, asset.id),),
            scene_style_id=style.scene.id,
            style_card_version_id=style.version.id,
            output_ratio="4:5",
            slot_key="detail",
            idempotency_key=run_key,
        )

        assert result.job.status == "machine_check"
    assert len(provider_calls) == 1


def _failed_real_paid_retry_job(
    session: Any, content_store: Any, asset: Any
) -> tuple[Any, ImageOutputJob, list[str]]:
    """Persist a real-mode failure that happened before any paid dispatch."""
    from test_classification_bridge_api import _real_generator

    style, sample_ids = _historical(session, content_store)
    card = _confirmed_identity_card(session, asset)
    generator = _real_generator([])

    def fail_before_provider(_: Any) -> Any:
        raise ImageGenerationError("pre_provider_failure")

    generator.generate = fail_before_provider  # type: ignore[method-assign]
    output = ImageJobService(session, content_store, generator).create(
        CUSTOMER_A_ID,
        output_type="product_style",
        sources=(ImageSourceSelection(asset.product_id, asset.id),),
        scene_style_id=style.scene.id,
        style_card_version_id=style.version.id,
        output_ratio="4:5",
        slot_key="detail",
        idempotency_key="paid-retry-child-key",
        **_identity_card_facts(card),
    )
    assert output.job.status == "generation_failed"
    return style, output.job, sample_ids


def test_new_cardless_nonpilot_job_cannot_reach_real_paid_dispatch(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    """A current cardless job cannot masquerade as a migration-era job."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    with api_session_factory() as session:
        style, job, _ = _failed_real_paid_retry_job(session, content_store, asset)
        job = _rewrite_job_as_preexisting_cardless_fixture(
            session, job, cohort_marker="cardless_non_pilot_v1"
        )
        run_key = "current-cardless-paid-scope"
        capability = issue_real_pilot_authorization(
            customer_id=CUSTOMER_A_ID,
            product_id=asset.product_id,
            source_asset_id=asset.id,
            scene_style_version_id=style.version.id,
            slot_key="detail",
            idempotency_key=run_key,
            authorized_usd=Decimal("1.00"),
        )
        session.add(
            RealImagePilotAuthorization(
                customer_id=CUSTOMER_A_ID,
                output_job_id=job.id,
                budget_scope=f"real_image_pilot:{job.id}",
                authorized_usd=Decimal("1.00"),
            )
        )
        session.commit()

        with pytest.raises(
            DomainRuleError, match="confirmed_product_identity_card_required"
        ):
            ImageJobService(
                session,
                content_store,
                _real_generator(provider_calls),
                real_pilot_authorization=capability,
                real_pilot_scope_key=run_key,
                enforce_real_pilot_boundary=True,
            ).retry_failed(CUSTOMER_A_ID, job.id)

        persisted = session.get(ImageOutputJob, job.id)
        assert persisted is not None
        assert persisted.status == "generation_failed"
    assert provider_calls == []


def test_default_real_dispatch_rejects_current_cardless_job_before_generator_or_http(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    """Dispatch identity validation is invariant even without the pilot flag."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    http_calls: list[httpx.Request] = []
    generator_calls: list[Any] = []
    with api_session_factory() as session:
        _, job, _ = _failed_real_paid_retry_job(session, content_store, asset)
        job = _rewrite_job_as_preexisting_cardless_fixture(
            session, job, cohort_marker="cardless_non_pilot_v1"
        )
        generator = _real_generator(http_calls)
        generate = generator.generate

        def record_generate(request: Any) -> Any:
            generator_calls.append(request)
            return generate(request)

        generator.generate = record_generate  # type: ignore[method-assign]

        with pytest.raises(
            DomainRuleError, match="confirmed_product_identity_card_required"
        ):
            ImageJobService(session, content_store, generator).retry_failed(
                CUSTOMER_A_ID, job.id
            )

        persisted = session.get(ImageOutputJob, job.id)
        assert persisted is not None
        assert persisted.status == "generation_failed"
    assert generator_calls == []
    assert http_calls == []


def test_markerless_migration_job_retains_legacy_paid_resume_compatibility(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    """Only rows lacking the post-migration cohort marker retain legacy recovery."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    with api_session_factory() as session:
        style, job, _ = _failed_real_paid_retry_job(session, content_store, asset)
        job = _rewrite_job_as_preexisting_cardless_fixture(
            session, job, cohort_marker=None
        )
        run_key = "migration-cardless-paid-scope"
        capability = issue_real_pilot_authorization(
            customer_id=CUSTOMER_A_ID,
            product_id=asset.product_id,
            source_asset_id=asset.id,
            scene_style_version_id=style.version.id,
            slot_key="detail",
            idempotency_key=run_key,
            authorized_usd=Decimal("1.00"),
        )
        session.add(
            RealImagePilotAuthorization(
                customer_id=CUSTOMER_A_ID,
                output_job_id=job.id,
                budget_scope=f"real_image_pilot:{job.id}",
                authorized_usd=Decimal("1.00"),
            )
        )
        session.commit()
        session.expire_all()
        legacy_job = session.get(ImageOutputJob, job.id)
        assert legacy_job is not None
        assert "product_identity_card_requirement" not in legacy_job.request_metadata

        result = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            real_pilot_authorization=capability,
            real_pilot_scope_key=run_key,
            enforce_real_pilot_boundary=True,
        ).retry_failed(CUSTOMER_A_ID, job.id)

        assert result.job.status == "machine_check"
    assert len(provider_calls) == 1


@pytest.mark.parametrize(
    "evidence",
    [
        "dispatch_uncertain",
        "provider_responded",
        "authorization_attempts",
        "audit_attempts",
    ],
)
def test_non_enforced_paid_retry_rejects_prior_dispatch_evidence_before_mutation(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
) -> None:
    """A direct real-service retry must never bypass durable no-repeat evidence."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    publisher_calls: list[tuple[Any, ...]] = []
    with api_session_factory() as session:
        _, job, _ = _failed_real_paid_retry_job(session, content_store, asset)
        authorization = _set_real_dispatch_evidence(session, job, evidence)
        session.expire_all()
        job = session.get(ImageOutputJob, job.id)
        authorization = session.get(RealImagePilotAuthorization, authorization.id)
        assert job is not None and authorization is not None
        before = _real_dispatch_state_snapshot(session, job, authorization)
        original_reserve = content_store.reserve_derivative

        def track_publish(*args: Any, **kwargs: Any) -> Any:
            publisher_calls.append(args)
            return original_reserve(*args, **kwargs)

        monkeypatch.setattr(content_store, "reserve_derivative", track_publish)
        service = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            enforce_real_pilot_boundary=False,
        )

        with pytest.raises(ImageGenerationOutcomeUncertainError):
            service.retry_failed(CUSTOMER_A_ID, job.id)

        session.expire_all()
        persisted = session.get(ImageOutputJob, job.id)
        persisted_authorization = session.get(
            RealImagePilotAuthorization, authorization.id
        )
        assert persisted is not None and persisted_authorization is not None
        assert (
            _real_dispatch_state_snapshot(session, persisted, persisted_authorization)
            == before
        )
    assert provider_calls == []
    assert publisher_calls == []


@pytest.mark.parametrize(
    "invalid_authorization",
    [
        "missing_capability",
        "tampered_capability",
        "mismatched_run_key",
        "local_demo_origin",
        "synthetic_evidence",
        "absent_ledger_row",
        "dispatch_uncertain",
        "positive_recorded_attempts",
    ],
)
def test_paid_retry_fails_closed_before_provider_or_publisher(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    invalid_authorization: str,
) -> None:
    """Every paid retry must re-prove route authority and unused durable budget."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    publisher_calls: list[tuple[Any, ...]] = []
    with api_session_factory() as session:
        style, job, sample_ids = _failed_real_paid_retry_job(
            session, content_store, asset
        )
        run_key = "paid-retry-run-key"
        capability = issue_real_pilot_authorization(
            customer_id=CUSTOMER_A_ID,
            product_id=asset.product_id,
            source_asset_id=asset.id,
            scene_style_version_id=style.version.id,
            slot_key="detail",
            idempotency_key=(
                "different-run-key"
                if invalid_authorization == "mismatched_run_key"
                else run_key
            ),
            authorized_usd=Decimal("1.00"),
        )
        if invalid_authorization == "missing_capability":
            capability = None
        elif invalid_authorization == "tampered_capability":
            capability = replace(capability, product_id="tampered-product")
        if invalid_authorization == "local_demo_origin":
            session.execute(
                update(SceneStyle)
                .where(SceneStyle.id == style.scene.id)
                .values(style_origin="local_demo")
            )
            version = session.get(StyleCardVersion, style.version.id)
            assert version is not None
            session.execute(
                update(StyleCardVersion)
                .where(StyleCardVersion.id == version.id)
                .values(content={**version.content, "style_origin": "local_demo"})
            )
        elif invalid_authorization == "synthetic_evidence":
            session.execute(
                update(StyleSample)
                .where(StyleSample.id.in_([UUID(value) for value in sample_ids]))
                .values(consent_provenance="local_synthetic_acceptance")
            )
        if invalid_authorization != "absent_ledger_row":
            session.add(
                RealImagePilotAuthorization(
                    customer_id=CUSTOMER_A_ID,
                    output_job_id=job.id,
                    budget_scope=f"real_image_pilot:{job.id}",
                    authorized_usd=Decimal("1.00"),
                    dispatch_state=(
                        "dispatch_uncertain"
                        if invalid_authorization == "dispatch_uncertain"
                        else "reserved"
                    ),
                    outbound_attempts=(
                        1
                        if invalid_authorization == "positive_recorded_attempts"
                        else 0
                    ),
                )
            )
        if invalid_authorization == "positive_recorded_attempts":
            session.add(
                AuditEvent(
                    customer_id=CUSTOMER_A_ID,
                    event_type="image_output.provider_dispatch_succeeded",
                    payload={"job_id": str(job.id), "outbound_attempts": 1},
                )
            )
        session.commit()

        original_reserve = content_store.reserve_derivative

        def track_publish(*args: Any, **kwargs: Any) -> Any:
            publisher_calls.append(args)
            return original_reserve(*args, **kwargs)

        monkeypatch.setattr(content_store, "reserve_derivative", track_publish)
        service = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            real_pilot_authorization=capability,
            real_pilot_scope_key=run_key,
            enforce_real_pilot_boundary=True,
        )
        expected_error = (
            ImageGenerationOutcomeUncertainError
            if invalid_authorization
            in {"dispatch_uncertain", "positive_recorded_attempts"}
            else DomainRuleError
        )
        with pytest.raises(expected_error):
            service.retry_failed(CUSTOMER_A_ID, job.id)

        persisted = session.get(ImageOutputJob, job.id)
        assert persisted is not None
        assert persisted.status == "generation_failed"
        assert persisted.failure_reason == "pre_provider_failure"
    assert provider_calls == []
    assert publisher_calls == []


def test_paid_retry_dispatches_once_with_fresh_capability_and_unused_reservation(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    """A provably pre-provider failure may consume its still-reserved budget once."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    with api_session_factory() as session:
        style, job, _ = _failed_real_paid_retry_job(session, content_store, asset)
        run_key = "paid-retry-run-key"
        capability = issue_real_pilot_authorization(
            customer_id=CUSTOMER_A_ID,
            product_id=asset.product_id,
            source_asset_id=asset.id,
            scene_style_version_id=style.version.id,
            slot_key="detail",
            idempotency_key=run_key,
            authorized_usd=Decimal("1.00"),
        )
        session.add(
            RealImagePilotAuthorization(
                customer_id=CUSTOMER_A_ID,
                output_job_id=job.id,
                budget_scope=f"real_image_pilot:{job.id}",
                authorized_usd=Decimal("1.00"),
            )
        )
        session.commit()

        result = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            real_pilot_authorization=capability,
            real_pilot_scope_key=run_key,
            enforce_real_pilot_boundary=True,
        ).retry_failed(CUSTOMER_A_ID, job.id)

        reservations = [
            event
            for event in session.scalars(select(AuditEvent))
            if event.event_type == "image_output.provider_dispatch_uncertain"
            and event.payload.get("job_id") == str(job.id)
        ]
        authorization = session.scalar(
            select(RealImagePilotAuthorization).where(
                RealImagePilotAuthorization.output_job_id == job.id
            )
        )
        assert result.job.status == "machine_check"
        assert len(provider_calls) == 1
        assert len(reservations) == 1
        assert authorization is not None
        assert authorization.dispatch_state == "provider_responded"
        assert authorization.outbound_attempts == 1


def test_paid_retry_validates_tampered_style_snapshot_before_any_mutation(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed sealed slot fact must not create misleading paid-attempt state."""
    from test_classification_bridge_api import _real_generator

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    publisher_calls: list[tuple[Any, ...]] = []
    with api_session_factory() as session:
        style, job, _ = _failed_real_paid_retry_job(session, content_store, asset)
        run_key = "paid-retry-run-key"
        capability = issue_real_pilot_authorization(
            customer_id=CUSTOMER_A_ID,
            product_id=asset.product_id,
            source_asset_id=asset.id,
            scene_style_version_id=style.version.id,
            slot_key="detail",
            idempotency_key=run_key,
            authorized_usd=Decimal("1.00"),
        )
        session.add(
            RealImagePilotAuthorization(
                customer_id=CUSTOMER_A_ID,
                output_job_id=job.id,
                budget_scope=f"real_image_pilot:{job.id}",
                authorized_usd=Decimal("1.00"),
            )
        )
        tampered_metadata = {
            **job.request_metadata,
            "slot_rules": {
                **job.request_metadata["slot_rules"],
                "camera_angle": "tampered-after-failure",
            },
        }
        session.execute(
            update(ImageOutputJob)
            .where(ImageOutputJob.id == job.id)
            .values(request_metadata=tampered_metadata)
        )
        session.commit()

        original_reserve = content_store.reserve_derivative

        def track_publish(*args: Any, **kwargs: Any) -> Any:
            publisher_calls.append(args)
            return original_reserve(*args, **kwargs)

        monkeypatch.setattr(content_store, "reserve_derivative", track_publish)
        service = ImageJobService(
            session,
            content_store,
            _real_generator(provider_calls),
            real_pilot_authorization=capability,
            real_pilot_scope_key=run_key,
            enforce_real_pilot_boundary=True,
        )

        with pytest.raises(
            DomainRuleError, match="image_generation_request_seal_mismatch"
        ):
            service.retry_failed(CUSTOMER_A_ID, job.id)

        persisted = session.get(ImageOutputJob, job.id)
        authorization = session.scalar(
            select(RealImagePilotAuthorization).where(
                RealImagePilotAuthorization.output_job_id == job.id
            )
        )
        reservations = [
            event
            for event in session.scalars(select(AuditEvent))
            if event.event_type == "image_output.provider_dispatch_uncertain"
            and event.payload.get("job_id") == str(job.id)
        ]
        assert persisted is not None
        assert persisted.status == "generation_failed"
        assert persisted.failure_reason == "pre_provider_failure"
        assert authorization is not None
        assert authorization.dispatch_state == "reserved"
        assert authorization.outbound_attempts == 0
        assert reservations == []
    assert provider_calls == []
    assert publisher_calls == []


@pytest.mark.parametrize(
    "invalid",
    [None, "withdrawn", "namespace", "missing", "tampered", "slot", "ratio"],
)
def test_style_reference_is_verified_separate_and_never_a_product_asset_link(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    invalid: str | None,
) -> None:
    asset = asset_factory()
    captured = []

    def handler(wire: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(budget="10"),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(10)),
    )
    original = generator.generate

    def capture(request: Any) -> Any:
        captured.append(request)
        return original(request)

    generator.generate = capture  # type: ignore[method-assign]
    with api_session_factory() as session:
        style, ids = _historical(session, content_store, 10)
        card = _confirmed_identity_card(session, asset)
        if invalid is None:
            # Other confirmed styles must never leak their samples into this version.
            _historical(session, content_store, name="unselected history")
        sample = session.scalar(
            select(StyleSample)
            .where(StyleSample.id.in_(UUID(sample_id) for sample_id in ids))
            .order_by(StyleSample.created_at, StyleSample.id)
        )
        assert sample is not None
        if invalid == "withdrawn":
            sample.withdrawn_at = datetime.now(UTC)
        elif invalid == "namespace":
            sample.asset_store_namespace = "another-store"
        elif invalid == "missing":
            content_store.discard_style_sample_content(CUSTOMER_A_ID, sample.id)
        elif invalid == "tampered":
            content_store.read_style_sample_content = lambda *args: b"tampered"
        session.commit()

        def run() -> Any:
            return ImageJobService(session, content_store, generator).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="1:1" if invalid == "ratio" else "4:5",
                slot_key="absent" if invalid == "slot" else "detail",
                idempotency_key="source-role-test",
                **_identity_card_facts(card),
            )

        if invalid:
            with pytest.raises((DomainRuleError, ImageGenerationError)) as error:
                run()
            assert "style_reference" in str(error.value) or "style_slot" in str(
                error.value
            )
            assert not captured
            return
        result = run()
        assert result.job.status == "machine_check"
        request = captured[0]
        assert [source.role for source in request.sources] == [
            "product_identity",
            *(["style_reference"] * 4),
        ]
        selected_reference_ids = {
            str(source.asset_id) for source in request.sources[1:]
        }
        assert len(selected_reference_ids) == 4
        assert selected_reference_ids < set(ids)
        assert all(source.product_id is None for source in request.sources[1:])
        assert request.main_style_card_version_id is None
        assert request.slot_key == "detail"
        assert request.slot_rules["camera_angle"] == "front"
        assert request.style_observations["tone"] == ["warm"]
        assert list(session.scalars(select(ProductAssetLink.product_asset_id))) == [
            asset.id
        ]
        assert result.job.request_metadata["style_reference_max_count"] == 4
        assert result.job.request_metadata["style_reference_pack_id"]
        assert request.style_reference_manifest_sha256 == result.job.request_metadata[
            "style_reference_manifest_sha256"
        ]
        assert [
            x["sample_id"] for x in result.job.request_metadata["style_references"]
        ] == [str(source.asset_id) for source in request.sources[1:]]


def test_verified_recovery_binding_allows_generation_reference_reads(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    asset = asset_factory()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(budget="10"),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(10)),
    )
    with api_session_factory() as session:
        style, _ = _historical(session, content_store, 10)
        card = _confirmed_identity_card(session, asset)
        legacy_namespace = "a" * 64
        session.execute(
            update(StyleSample).values(asset_store_namespace=legacy_namespace)
        )
        session.add(
            AssetStoreRecoveryBinding(
                id=uuid4(),
                customer_id=CUSTOMER_A_ID,
                source_namespace=legacy_namespace,
                target_namespace=content_store.store_namespace,
                sample_count=10,
                manifest_sha256="b" * 64,
                source_label="verified-copy",
                status="active",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()

        result = ImageJobService(session, content_store, generator).create(
            CUSTOMER_A_ID,
            output_type="product_style",
            sources=(ImageSourceSelection(asset.product_id, asset.id),),
            scene_style_id=style.scene.id,
            style_card_version_id=style.version.id,
            output_ratio="4:5",
            slot_key="detail",
            idempotency_key="recovered-reference-read",
            **_identity_card_facts(card),
        )

        assert result.job.status == "machine_check"


def test_style_reference_over_limit_uses_confirmed_evidence_and_records_scope(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    """Large style libraries reach generation through one sealed four-image pack."""
    asset = asset_factory()
    captured = []

    def handler(wire: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(budget="10"),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(10)),
    )
    original = generator.generate

    def capture(request: Any) -> Any:
        captured.append(request)
        return original(request)

    generator.generate = capture  # type: ignore[method-assign]
    with api_session_factory() as session:
        style, _ = _historical(session, content_store, 16)
        card = _confirmed_identity_card(session, asset)
        chronological_ids = [
            str(row.id)
            for row in session.scalars(
                select(StyleSample).order_by(StyleSample.created_at, StyleSample.id)
            )
        ]
        expected_ids = chronological_ids[:4]

        result = ImageJobService(session, content_store, generator).create(
            CUSTOMER_A_ID,
            output_type="product_style",
            sources=(ImageSourceSelection(asset.product_id, asset.id),),
            scene_style_id=style.scene.id,
            style_card_version_id=style.version.id,
            output_ratio="4:5",
            slot_key="detail",
            idempotency_key="timeline-anchor-selection",
            **_identity_card_facts(card),
        )

        assert result.job.status == "machine_check"
        assert len(captured) == 1
        assert [str(source.asset_id) for source in captured[0].sources[1:]] == (
            expected_ids
        )
        assert result.job.request_metadata["style_reference_total_count"] == 16
        assert result.job.request_metadata["style_reference_selected_count"] == 4
        assert result.job.request_metadata["style_reference_selection"] == (
            "sealed_style_reference_pack_v1"
        )
        assert result.job.request_metadata["style_reference_pack_id"]
        assert [
            item["sample_id"]
            for item in result.job.request_metadata["style_references"]
        ] == expected_ids
        assert result.candidates
        for candidate in result.candidates:
            assert candidate.artifact_metadata["provider_attachment_count"] == 5
            assert (
                candidate.artifact_metadata[
                    "provider_product_identity_attachment_count"
                ]
                == 1
            )
            assert (
                candidate.artifact_metadata[
                    "provider_style_reference_attachment_count"
                ]
                == 4
            )
            assert candidate.artifact_metadata[
                "style_reference_manifest_sha256"
            ] == result.job.request_metadata["style_reference_manifest_sha256"]


def test_real_product_style_rejects_incomplete_structured_rules_before_http(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    asset = asset_factory()
    provider_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(200, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(budget="10"),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(10)),
    )
    with api_session_factory() as session:
        style, _ = _historical(session, content_store)
        card = _confirmed_identity_card(session, asset)
        version = session.get(StyleCardVersion, style.version.id)
        assert version is not None
        session.execute(
            update(StyleCardVersion)
            .where(StyleCardVersion.id == version.id)
            .values(
                content={
                    **version.content,
                    "overrides": {**version.content.get("overrides", {}), "palette": []},
                }
            )
        )
        session.commit()

        with pytest.raises(DomainRuleError, match="structured_style_rules_required"):
            ImageJobService(session, content_store, generator).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key="incomplete-structured-rules",
                **_identity_card_facts(card),
            )

    assert provider_calls == []


def test_style_reference_selection_checks_withdrawn_unselected_evidence(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    """Withdrawing an unselected photo must still block the sealed style."""
    asset = asset_factory()
    provider_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(200, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(budget="10"),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(10)),
    )
    with api_session_factory() as session:
        style, _ = _historical(session, content_store, 16)
        card = _confirmed_identity_card(session, asset)
        chronological = list(
            session.scalars(
                select(StyleSample).order_by(StyleSample.created_at, StyleSample.id)
            )
        )
        chronological[7].withdrawn_at = datetime.now(UTC)
        session.commit()

        with pytest.raises(
            DomainRuleError, match="style_reference_permission_unavailable"
        ):
            ImageJobService(session, content_store, generator).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=style.scene.id,
                style_card_version_id=style.version.id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key="withdrawn-unselected-reference",
                **_identity_card_facts(card),
            )
        assert provider_calls == []


def test_legacy_product_style_route_rejects_real_request_without_identity_card(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    from fastapi import Response

    from opc_style.api.routes_image_jobs import _create_output
    from opc_style.schemas import ImageGenerationOutputRequest

    asset = asset_factory()
    provider_calls: list[httpx.Request] = []
    with api_session_factory() as session:
        style, _ = _historical(session, content_store)
        scene_id, version_id = style.scene.id, style.version.id
    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: provider_calls.append(request)
                or httpx.Response(200, json=_success_payload())
            )
        ),
        budget=RealImageBudget(Decimal(1)),
    )
    body = {
        "sources": [{"product_id": asset.product_id, "asset_id": str(asset.id)}],
        "scene_style_id": str(scene_id),
        "style_card_version_id": str(version_id),
        "output_ratio": "4:5",
        "slot_key": "detail",
        "idempotency_key": "role-api",
    }
    capability = issue_real_pilot_authorization(
        customer_id=CUSTOMER_A_ID,
        product_id=asset.product_id,
        source_asset_id=asset.id,
        scene_style_version_id=version_id,
        slot_key="detail",
        idempotency_key="role-api",
        authorized_usd=Decimal("1.00"),
    )
    with api_session_factory() as session:
        response = _create_output(
            ImageGenerationOutputRequest.model_validate(body),
            CUSTOMER_A_ID,
            session,
            content_store,
            generator,
            Response(),
            output_type="product_style",
            real_pilot_authorization=capability,
        )
        assert response.status_code == 409
        assert json.loads(bytes(response.body))["error"]["code"] == (
            "confirmed_product_identity_card_required"
        )
    assert provider_calls == []


@pytest.mark.parametrize(
    ("invalid", "expected_code", "expected_status"),
    [
        ("slot", "confirmed_product_identity_card_required", 409),
        ("withdrawn", "confirmed_product_identity_card_required", 409),
        ("ratio", "confirmed_product_identity_card_required", 409),
        ("identities", "generation_source_identity_count", 409),
        ("unconfirmed", "confirmed_product_identity_card_required", 409),
        ("slot_seal", "confirmed_product_identity_card_required", 409),
        ("namespace", "confirmed_product_identity_card_required", 409),
        ("missing", "confirmed_product_identity_card_required", 409),
        ("tampered", "confirmed_product_identity_card_required", 409),
        ("reference_seal", "confirmed_product_identity_card_required", 409),
        ("storage_exception", "confirmed_product_identity_card_required", 409),
    ],
)
def test_legacy_product_style_route_fails_closed_before_deeper_style_checks(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
    expected_code: str,
    expected_status: int,
) -> None:
    from fastapi import Response

    from opc_style.api.routes_image_jobs import _create_output
    from opc_style.models import StyleCardVersion, StyleTemplateSlot
    from opc_style.schemas import ImageGenerationOutputRequest
    from opc_style.services.real_photos import AssetContentStoreError

    asset = asset_factory()
    with api_session_factory() as session:
        style, _ = _historical(session, content_store, 2)
        scene_id, version_id = style.scene.id, style.version.id
        sample = session.scalars(select(StyleSample)).first()
        if invalid == "withdrawn":
            sample.withdrawn_at = datetime.now(UTC)
        elif invalid == "namespace":
            sample.asset_store_namespace = "another-store"
        elif invalid == "unconfirmed":
            # Seed invalid stored facts directly; normal ORM writes are immutable.
            session.execute(
                update(StyleCardVersion)
                .where(StyleCardVersion.id == version_id)
                .values(content={**style.version.content, "stability": "draft"})
            )
        elif invalid == "slot_seal":
            slot = session.scalars(select(StyleTemplateSlot)).first()
            session.execute(
                update(StyleTemplateSlot)
                .where(StyleTemplateSlot.id == slot.id)
                .values(rules={**slot.rules, "camera_angle": "changed"})
            )
        elif invalid == "missing":
            content_store.discard_style_sample_content(CUSTOMER_A_ID, sample.id)
        elif invalid == "tampered":
            monkeypatch.setattr(
                content_store, "read_style_sample_content", lambda *args: b"tampered"
            )
        elif invalid == "storage_exception":

            def unavailable(*args: Any) -> bytes:
                raise AssetContentStoreError(
                    "private path /private/customer-photo.jpg test-secret-value"
                )

            monkeypatch.setattr(content_store, "read_style_sample_content", unavailable)
        session.commit()

    if invalid == "reference_seal":
        original_read = content_store.read_original

        def change_seal_after_product_read(*args: Any) -> bytes:
            content = original_read(*args)
            with api_session_factory() as session:
                row = session.scalars(select(StyleSample)).first()
                row.consent_text_version = "changed-after-job-seal"
                session.commit()
            return content

        monkeypatch.setattr(
            content_store, "read_original", change_seal_after_product_read
        )

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(1)),
    )
    source = {"product_id": asset.product_id, "asset_id": str(asset.id)}
    slot_key = "absent" if invalid == "slot" else "detail"
    idempotency_key = f"new-business-rejection-{invalid}"
    request = ImageGenerationOutputRequest.model_validate(
        {
            "sources": [source, source] if invalid == "identities" else [source],
            "scene_style_id": str(scene_id),
            "style_card_version_id": str(version_id),
            "output_ratio": "1:1" if invalid == "ratio" else "4:5",
            "slot_key": slot_key,
            "idempotency_key": idempotency_key,
        }
    )
    capability = issue_real_pilot_authorization(
        customer_id=CUSTOMER_A_ID,
        product_id=asset.product_id,
        source_asset_id=asset.id,
        scene_style_version_id=version_id,
        slot_key=slot_key,
        idempotency_key=idempotency_key,
        authorized_usd=Decimal("1.00"),
    )
    with api_session_factory() as session:
        response = _create_output(
            request,
            CUSTOMER_A_ID,
            session,
            content_store,
            generator,
            Response(),
            output_type="product_style",
            real_pilot_authorization=capability,
        )
    assert response.status_code == expected_status, bytes(response.body)
    assert json.loads(bytes(response.body)) == {
        "error": {"code": expected_code, "retryable": expected_status == 503}
    }
    for sensitive in (
        "test-secret-value",
        "/private/",
        "another-store",
        "changed-after-job-seal",
    ):
        assert sensitive.encode() not in bytes(response.body)
    assert calls == []


def test_source_role_local_bridge_fingerprint_includes_slot_rules() -> None:
    request = _request()
    generator = DeterministicLocalImageGenerator()
    hero = generator.generate(replace(request, slot_key="hero"))
    detail = generator.generate(
        replace(request, slot_key="detail", slot_rules={"camera_angle": "side"})
    )
    assert hero[0].content_sha256 != detail[0].content_sha256


def test_source_role_legacy_scene_resolves_pinned_main_observations(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    style_factory: Any,
) -> None:
    _, scene, version = style_factory()
    asset = asset_factory()
    with api_session_factory() as session:
        result = ImageJobService(
            session, content_store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=(ImageSourceSelection(asset.product_id, asset.id),),
            scene_style_id=scene.id,
            style_card_version_id=version.id,
            output_ratio="1:1",
            idempotency_key="legacy-observations",
        )
        assert result.job.request_metadata["style_observations"]["tone"] == [
            "synthetic-quiet"
        ]


def test_style_reference_withdrawal_after_byte_load_prevents_dispatch(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = asset_factory()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(1)),
    )
    with api_session_factory() as session:
        style, _ = _historical(session, content_store)
        card = _confirmed_identity_card(session, asset)
        original = content_store.read_style_sample_content

        def withdraw_after_read(customer: Any, sample_id: Any, digest: Any) -> bytes:
            content = original(customer, sample_id, digest)
            StyleSampleService(session, content_store).withdraw(customer, sample_id)
            return content

        monkeypatch.setattr(
            content_store, "read_style_sample_content", withdraw_after_read
        )
        result = ImageJobService(session, content_store, generator).create(
            CUSTOMER_A_ID,
            output_type="product_style",
            sources=(ImageSourceSelection(asset.product_id, asset.id),),
            scene_style_id=style.scene.id,
            style_card_version_id=style.version.id,
            output_ratio="4:5",
            slot_key="detail",
            idempotency_key="withdraw-after-read",
            **_identity_card_facts(card),
        )
        assert result.job.status == "generation_failed"
        assert result.job.failure_reason == "style_reference_permission_unavailable"
        assert calls == []


def test_style_reference_consent_withdrawal_waits_for_started_provider_call(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: Any,
) -> None:
    from threading import Event, Thread
    from uuid import UUID

    asset = asset_factory()
    with api_session_factory() as session:
        style, ids = _historical(session, content_store)
        scene_id, version_id = style.scene.id, style.version.id
        card_facts = _identity_card_facts(_confirmed_identity_card(session, asset))
    attempted, withdrawal_started, finished = Event(), Event(), Event()
    errors = []

    original_withdraw = StyleSampleService._withdraw

    def observed_withdraw(
        service: StyleSampleService, customer_id: UUID, sample_id: UUID
    ) -> Any:
        withdrawal_started.set()
        return original_withdraw(service, customer_id, sample_id)

    monkeypatch.setattr(StyleSampleService, "_withdraw", observed_withdraw)

    def withdraw() -> None:
        try:
            with api_session_factory() as session:
                attempted.set()
                StyleSampleService(session, content_store).withdraw(
                    CUSTOMER_A_ID, UUID(ids[0])
                )
        except Exception as error:  # noqa: BLE001 - surface worker failures to test thread
            errors.append(error)
        finally:
            finished.set()

    worker = Thread(target=withdraw)
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        worker.start()
        assert attempted.wait(timeout=2)
        observed.append(finished.wait(timeout=0.05))
        return httpx.Response(200, json=_success_payload())

    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(1)),
    )
    dispatch_observed = []
    original_record_dispatch = ImageJobService._record_provider_dispatch

    def observed_record_dispatch(*args: Any, **kwargs: Any) -> Any:
        dispatch_observed.append(withdrawal_started.wait(timeout=0.2))
        return original_record_dispatch(*args, **kwargs)

    monkeypatch.setattr(
        ImageJobService, "_record_provider_dispatch", observed_record_dispatch
    )
    try:
        with api_session_factory() as session:
            result = ImageJobService(session, content_store, generator).create(
                CUSTOMER_A_ID,
                output_type="product_style",
                sources=(ImageSourceSelection(asset.product_id, asset.id),),
                scene_style_id=scene_id,
                style_card_version_id=version_id,
                output_ratio="4:5",
                slot_key="detail",
                idempotency_key="withdraw-during-provider",
                **card_facts,
            )
            assert result.job.status == "machine_check"
    finally:
        worker.join(timeout=5)
    assert finished.is_set()
    assert errors == []
    assert observed == [False]
    assert dispatch_observed == [False]


def test_real_product_style_without_confirmed_card_never_calls_generator(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    provider_calls: list[httpx.Request] = []
    asset = asset_factory()
    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    provider_calls.append(request)
                    or httpx.Response(200, request=request, json=_success_payload())
                )
            )
        ),
        budget=RealImageBudget(Decimal("1.00")),
    )
    with api_session_factory() as session:
        style, _ = _historical(session, content_store)
        key = "missing-confirmed-card"
        capability = issue_real_pilot_authorization(
            customer_id=CUSTOMER_A_ID,
            product_id=asset.product_id,
            source_asset_id=asset.id,
            scene_style_version_id=style.version.id,
            slot_key="detail",
            idempotency_key=key,
            authorized_usd=Decimal("1.00"),
        )
        service = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=generator,
            real_pilot_authorization=capability,
            enforce_real_pilot_boundary=True,
            required_style_origin="customer_upload",
        )

        with pytest.raises(
            DomainRuleError, match="confirmed_product_identity_card_required"
        ):
            service.create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key=key,
                pilot_scope=StyleSuitePilotScope(
                    scene_style_version_id=style.version.id,
                    slot_key="detail",
                ),
                product_identity_card_version_id=None,
            )

        assert list(session.scalars(select(ImageOutputJob))) == []
    assert provider_calls == []


def test_card_scope_mismatch_is_rejected_before_job_or_generator_dispatch(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> None:
    provider_calls: list[httpx.Request] = []
    asset = asset_factory()
    other_asset = asset_factory(product_id="product-002")
    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    provider_calls.append(request)
                    or httpx.Response(200, request=request, json=_success_payload())
                )
            )
        ),
        budget=RealImageBudget(Decimal("1.00")),
    )
    with api_session_factory() as session:
        style, _ = _historical(session, content_store)
        foreign_card = _confirmed_identity_card(session, other_asset)
        key = "foreign-card"

        class RecordingCapability:
            calls = 0

            def authorizes_suite(self, **facts: Any) -> bool:
                del facts
                self.calls += 1
                return True

            def authorizes_job(self, **facts: Any) -> bool:
                del facts
                return True

        capability = RecordingCapability()
        service = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=generator,
            real_pilot_authorization=capability,  # type: ignore[arg-type]
            enforce_real_pilot_boundary=True,
            required_style_origin="customer_upload",
        )

        with pytest.raises(
            DomainRuleError, match="product_identity_card_scope_mismatch"
        ):
            service.create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key=key,
                pilot_scope=StyleSuitePilotScope(
                    scene_style_version_id=style.version.id,
                    slot_key="detail",
                ),
                product_identity_card_version_id=foreign_card.id,
            )

        assert list(session.scalars(select(ImageOutputJob))) == []
        assert capability.calls == 0
    assert provider_calls == []


@pytest.mark.parametrize("tamper", ["card_digest", "source_bytes"])
def test_card_scope_rehashes_current_card_and_source_before_authorization(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    provider_calls: list[httpx.Request] = []
    asset = asset_factory()
    generator = OpenAIImageGenerator(
        _settings(),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    provider_calls.append(request)
                    or httpx.Response(200, request=request, json=_success_payload())
                )
            )
        ),
        budget=RealImageBudget(Decimal("1.00")),
    )
    with api_session_factory() as session:
        style, _ = _historical(session, content_store)
        card = _confirmed_identity_card(session, asset)
        if tamper == "card_digest":
            session.execute(
                update(ProductIdentityCardVersion)
                .where(ProductIdentityCardVersion.id == card.id)
                .values(card_sha256="f" * 64)
            )
            session.commit()
        else:
            monkeypatch.setattr(
                content_store,
                "read_original",
                lambda customer_id, asset_id, digest: b"tampered-source-bytes",
            )

        class RecordingCapability:
            calls = 0

            def authorizes_suite(self, **facts: Any) -> bool:
                del facts
                self.calls += 1
                return True

            def authorizes_job(self, **facts: Any) -> bool:
                del facts
                return True

        capability = RecordingCapability()
        service = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=generator,
            real_pilot_authorization=capability,  # type: ignore[arg-type]
            enforce_real_pilot_boundary=True,
            required_style_origin="customer_upload",
        )

        with pytest.raises(DomainRuleError, match="product_identity_card_scope_mismatch"):
            service.create(
                CUSTOMER_A_ID,
                asset.product_id,
                asset.id,
                idempotency_key=f"tampered-{tamper}",
                pilot_scope=StyleSuitePilotScope(
                    scene_style_version_id=style.version.id,
                    slot_key="detail",
                ),
                product_identity_card_version_id=card.id,
            )

        assert capability.calls == 0
        assert list(session.scalars(select(ImageOutputJob))) == []
    assert provider_calls == []
