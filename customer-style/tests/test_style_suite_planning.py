"""Suite planning must pin every allowed slot without generating or writing."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from opc_style.api.dependencies import get_session
from opc_style.db import create_database_engine
from opc_style.domain.errors import DomainRuleError
from opc_style.main import create_app
from opc_style.models import (
    Base,
    Customer,
    ProductAsset,
    ProductAssetPermissionEvent,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
    StyleFieldEvidence,
    StyleSample,
)
from opc_style.schemas import HistoricalStyleGroupsConfirmRequest, StyleSuitePilotScope
from opc_style.services.styles import HistoricalStyleConfirmationService

A = UUID("11111111-1111-1111-1111-111111111111")
B = UUID("22222222-2222-2222-2222-222222222222")
HEADERS = {"X-Customer-Id": str(A)}


def test_real_confirmation_seals_reviewed_rules_and_field_evidence(
    session: Session,
) -> None:
    session.add(Customer(id=A, display_name="A"))
    samples = []
    for index in range(10):
        sample = StyleSample(
            customer_id=A,
            photo_id=f"real-history-{index}",
            authorized_at=datetime.now(UTC),
            consent_provenance="local-explicit-style-upload",
            consent_text_version="local-style-upload-v1",
            content_sha256=f"{index:064x}",
            mime_type="image/jpeg",
            width=100,
            height=100,
            asset_store_namespace="a" * 64,
        )
        session.add(sample)
        samples.append(sample)
    session.commit()
    evidence_ids = [sample.id for sample in samples]
    field_evidence = {
        field: evidence_ids
        for field in ("palette", "lighting", "composition", "backgrounds")
    }
    request = HistoricalStyleGroupsConfirmRequest.model_validate(
        {
            "confirmation_key": "real-reviewed-style-v1",
            "confirmed": True,
            "groups": [
                {
                    "name": "暖白桌面",
                    "proposal_keys": ["warm-square"],
                    "evidence_sample_ids": evidence_ids,
                    "analysis_mode": "real_visual_analysis_proposal",
                    "analyzer_version": "openai-style-analysis-v1",
                    "proposal_fingerprint": "f" * 64,
                    "reference_selection_version": "pixel-diversity-farthest-v1",
                    "reference_manifest_sha256": "e" * 64,
                    "palette": ["暖白", "低饱和金色"],
                    "lighting": ["柔和左侧漫射光"],
                    "composition": ["主体居中并保留上方留白"],
                    "backgrounds": ["暖白无缝背景"],
                    "field_evidence": field_evidence,
                    "slots": [
                        {
                            "slot_key": "hero",
                            "label": "主图",
                            "rules": {
                                "aspect_ratio": "4:5",
                                "shot_distance": "close",
                                "camera_angle": "front",
                                "composition": ["centered"],
                                "background": ["warm white"],
                                "lighting": ["soft left"],
                            },
                        }
                    ],
                }
            ],
        }
    )

    snapshot = HistoricalStyleConfirmationService(session).confirm(A, request)[0]

    assert snapshot.version.content["lighting"] == ["柔和左侧漫射光"]
    assert snapshot.version.content["composition"] == ["主体居中并保留上方留白"]
    assert snapshot.version.content["analysis_mode"] == "real_visual_analysis_proposal"
    assert (
        snapshot.version.content["reference_selection_version"]
        == "pixel-diversity-farthest-v1"
    )
    assert snapshot.version.content["reference_manifest_sha256"] == "e" * 64
    evidence = list(
        session.scalars(
            select(StyleFieldEvidence).where(
                StyleFieldEvidence.style_card_version_id == snapshot.version.id
            )
        )
    )
    assert {row.field_name for row in evidence} == {
        "palette",
        "lighting",
        "composition",
        "backgrounds",
    }
    assert len(evidence) == 50

    from opc_style.services.style_suites import StyleSuiteService
    from opc_style.settings import StyleSuitePlanningSettings

    source = setup_source(session)
    plan = StyleSuiteService(
        session,
        settings=StyleSuitePlanningSettings(
            candidate_estimate_usd=Decimal("0.12"),
            real_generation_enabled=True,
        ),
        enforce_real_pilot_boundary=True,
        required_style_origin="customer_upload",
    ).preview(A, source.product_id, source.id)
    assert len(plan.items) == 1

    synthetic_request = request.model_copy(
        update={
            "confirmation_key": "synthetic-cannot-be-real-v1",
            "groups": [
                request.groups[0].model_copy(
                    update={
                        "name": "伪真实风格",
                        "analysis_mode": "synthetic_test_only",
                    }
                )
            ],
        }
    )
    with pytest.raises(
        DomainRuleError, match="historical_style_real_analysis_required"
    ):
        HistoricalStyleConfirmationService(session).confirm(A, synthetic_request)


def setup_source(session: Session, **changes: Any) -> ProductAsset:
    permission = changes.pop("permission", True)
    for owner in (A, B):
        if session.get(Customer, owner) is None:
            session.add(Customer(id=owner, display_name=str(owner)))
    session.flush()
    facts = {
        "customer_id": A,
        "product_id": "product-001",
        "photo_id": str(uuid4()),
        "confirmed": True,
        "source_type": "synthetic_metadata",
        "quality_flags": [],
        "occurred_at": datetime.now(UTC),
        "content_sha256": "a" * 64,
        "mime_type": "image/jpeg",
        "width": 1200,
        "height": 900,
    }
    asset = ProductAsset(**(facts | changes))
    session.add(asset)
    session.flush()
    if permission:
        session.add(
            ProductAssetPermissionEvent(
                customer_id=asset.customer_id,
                product_asset_id=asset.id,
                transform_permission=True,
                occurred_at=datetime.now(UTC),
                provenance="synthetic-test",
            )
        )
    session.commit()
    return asset


def slot(key: str, active: bool = True) -> dict[str, Any]:
    return {
        "slot_key": key,
        "label": key,
        "active": active,
        "rules": {
            "aspect_ratio": "4:5",
            "shot_distance": "close",
            "camera_angle": "front",
            "background": ["white"],
        },
    }


def confirm(
    session: Session,
    name: str,
    keys: list[str],
    owner: UUID = A,
    *,
    consent_provenance: str = "synthetic-test",
    style_origin: str = "customer_upload",
    confirmation_key: str | None = None,
    evidence_sample_id: UUID | None = None,
) -> Any:
    if session.get(Customer, owner) is None:
        session.add(Customer(id=owner, display_name=str(owner)))
        session.commit()
    if evidence_sample_id is None:
        sample = StyleSample(
            customer_id=owner,
            photo_id=str(uuid4()),
            authorized_at=datetime.now(UTC),
            consent_provenance=consent_provenance,
            consent_text_version="v1",
            content_sha256="b" * 64,
            mime_type="image/jpeg",
            width=10,
            height=12,
            asset_store_namespace="synthetic-only",
        )
        session.add(sample)
        session.commit()
        evidence_sample_id = sample.id
    request = HistoricalStyleGroupsConfirmRequest.model_validate(
        {
            "confirmation_key": confirmation_key or str(uuid4()),
            "confirmed": True,
            "groups": [
                {
                    "name": name,
                    "evidence_sample_ids": [str(evidence_sample_id)],
                    "tone": ["warm"],
                    "slots": [slot(key) for key in keys],
                }
            ],
        }
    )
    return HistoricalStyleConfirmationService(session).confirm(
        owner, request, style_origin=style_origin
    )[0]


def test_confirmation_seals_customer_uploaded_style_origin(session: Session) -> None:
    """Removing the origin seal must make the paid-style boundary observable."""
    confirmed = confirm(session, "history", ["hero"])

    assert confirmed.version.content["style_origin"] == "customer_upload"


def test_exact_proposal_reconfirmation_reuses_existing_scenes_at_limit(
    session: Session,
) -> None:
    """Replaying one reviewed proposal must not consume three more scene slots."""
    confirm(session, "existing-one", ["hero"])
    confirm(session, "existing-two", ["detail"])
    samples = []
    for index in range(10):
        sample = StyleSample(
            customer_id=A,
            photo_id=f"reconfirmation-history-{index}",
            authorized_at=datetime.now(UTC),
            consent_provenance="local-explicit-style-upload",
            consent_text_version="local-style-upload-v1",
            content_sha256=f"{index + 100:064x}",
            mime_type="image/jpeg",
            width=100,
            height=100,
            asset_store_namespace="reconfirmation-test",
        )
        session.add(sample)
        samples.append(sample)
    session.commit()
    evidence_ids = [sample.id for sample in samples]
    groups = [
        {
            "name": name,
            "proposal_keys": [f"proposal-{index}"],
            "evidence_sample_ids": evidence_ids,
            "analysis_mode": "real_visual_analysis_proposal",
            "analyzer_version": "openai-style-analysis-v1",
            "proposal_fingerprint": "f" * 64,
            "reference_selection_version": "pixel-diversity-farthest-v1",
            "reference_manifest_sha256": "e" * 64,
            "palette": ["红色"],
            "lighting": ["柔光"],
            "composition": ["居中构图"],
            "backgrounds": ["纯色背景"],
            "field_evidence": {
                field: evidence_ids
                for field in ("palette", "lighting", "composition", "backgrounds")
            },
            "slots": [
                {
                    "slot_key": slot_key,
                    "label": slot_key,
                    "rules": {
                        "aspect_ratio": "4:5",
                        "shot_distance": "close",
                        "camera_angle": "front",
                        "composition": ["居中构图"],
                        "background": ["纯色背景"],
                        "lighting": ["柔光"],
                    },
                }
            ],
        }
        for index, (name, slot_key) in enumerate(
            (
                ("红调环境背景", "hero"),
                ("红调纯色背景", "hero"),
                ("红调细节", "detail"),
            )
        )
    ]
    first_request = HistoricalStyleGroupsConfirmRequest.model_validate(
        {
            "confirmation_key": "reviewed-proposal-first-confirmation",
            "confirmed": True,
            "groups": groups,
        }
    )
    first = HistoricalStyleConfirmationService(session).confirm(A, first_request)
    replay_request = first_request.model_copy(
        update={"confirmation_key": "reviewed-proposal-browser-replay"}
    )

    replayed = HistoricalStyleConfirmationService(session).confirm(A, replay_request)

    assert [snapshot.scene.id for snapshot in replayed] == [
        snapshot.scene.id for snapshot in first
    ]
    assert (
        session.scalar(
            select(func.count())
            .select_from(SceneStyle)
            .where(
                SceneStyle.customer_id == A,
                SceneStyle.style_origin == "customer_upload",
                SceneStyle.active.is_(True),
            )
        )
        == 5
    )
    changed_request = first_request.model_copy(
        update={
            "confirmation_key": "reviewed-proposal-changed-name",
            "groups": [
                first_request.groups[0].model_copy(update={"name": "人工改名后的新风格"}),
                *first_request.groups[1:],
            ],
        }
    )
    with pytest.raises(DomainRuleError, match="scene_limit_reached"):
        HistoricalStyleConfirmationService(session).confirm(A, changed_request)


def test_demo_scenes_do_not_consume_the_real_customer_scene_limit(
    session: Session,
) -> None:
    """Legacy demo scenes must not reduce the five real customer scene slots."""
    for index in range(2):
        confirm(
            session,
            f"legacy-demo-{index}",
            ["hero"],
            consent_provenance="local_synthetic_acceptance",
            style_origin="local_demo",
        )
    real_sample = StyleSample(
        customer_id=A,
        photo_id=str(uuid4()),
        authorized_at=datetime.now(UTC),
        consent_provenance="customer-consent-v2",
        consent_text_version="v1",
        content_sha256="d" * 64,
        mime_type="image/jpeg",
        width=10,
        height=12,
        asset_store_namespace="customer-upload",
    )
    session.add(real_sample)
    session.commit()
    five_real_groups = HistoricalStyleGroupsConfirmRequest.model_validate(
        {
            "confirmation_key": str(uuid4()),
            "confirmed": True,
            "groups": [
                {
                    "name": f"real-style-{index}",
                    "evidence_sample_ids": [str(real_sample.id)],
                    "tone": ["warm"],
                    "slots": [slot("hero")],
                }
                for index in range(5)
            ],
        }
    )

    confirmed = HistoricalStyleConfirmationService(session).confirm(
        A,
        five_real_groups,
        style_origin="customer_upload",
    )

    assert len(confirmed) == 5
    assert {row.version.content["style_origin"] for row in confirmed} == {
        "customer_upload"
    }


@pytest.mark.parametrize(
    ("consent_provenance", "style_origin"),
    [
        ("local_synthetic_acceptance", "customer_upload"),
        ("customer-consent-v2", "local_demo"),
    ],
)
def test_confirmation_rejects_evidence_from_the_other_style_origin(
    session: Session, consent_provenance: str, style_origin: str
) -> None:
    """Demo evidence cannot become paid style evidence, or vice versa."""
    with pytest.raises(DomainRuleError, match="historical_style_origin_mismatch"):
        confirm(
            session,
            "history",
            ["hero"],
            consent_provenance=consent_provenance,
            style_origin=style_origin,
        )


def test_confirmation_rejects_mixed_customer_and_demo_evidence(
    session: Session,
) -> None:
    """One demo sample must poison the whole paid-style evidence group."""
    setup_source(session)
    customer_sample = StyleSample(
        customer_id=A,
        photo_id=str(uuid4()),
        authorized_at=datetime.now(UTC),
        consent_provenance="customer-consent-v2",
        consent_text_version="v1",
        content_sha256="b" * 64,
        mime_type="image/jpeg",
        width=10,
        height=12,
        asset_store_namespace="synthetic-only",
    )
    demo_sample = StyleSample(
        customer_id=A,
        photo_id=str(uuid4()),
        authorized_at=datetime.now(UTC),
        consent_provenance="local_synthetic_acceptance",
        consent_text_version="v1",
        content_sha256="c" * 64,
        mime_type="image/jpeg",
        width=10,
        height=12,
        asset_store_namespace="synthetic-only",
    )
    session.add_all([customer_sample, demo_sample])
    session.commit()
    request = HistoricalStyleGroupsConfirmRequest.model_validate(
        {
            "confirmation_key": str(uuid4()),
            "confirmed": True,
            "groups": [
                {
                    "name": "mixed-origin",
                    "evidence_sample_ids": [
                        str(customer_sample.id),
                        str(demo_sample.id),
                    ],
                    "tone": ["warm"],
                    "slots": [slot("hero")],
                }
            ],
        }
    )

    with pytest.raises(DomainRuleError, match="historical_style_origin_mismatch"):
        HistoricalStyleConfirmationService(session).confirm(A, request)


def test_confirmation_key_cannot_be_replayed_with_a_different_style_origin(
    session: Session,
) -> None:
    """Changing origin under one receipt key must conflict instead of replaying."""
    confirmation_key = str(uuid4())
    confirm(
        session,
        "history",
        ["hero"],
        confirmation_key=confirmation_key,
    )
    evidence_sample_id = session.scalar(
        select(StyleSample.id).where(StyleSample.customer_id == A)
    )
    assert evidence_sample_id is not None

    with pytest.raises(DomainRuleError, match="style_confirmation_conflict"):
        confirm(
            session,
            "history",
            ["hero"],
            consent_provenance="local_synthetic_acceptance",
            style_origin="local_demo",
            confirmation_key=confirmation_key,
            evidence_sample_id=evidence_sample_id,
        )


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    app = create_app()
    engine = create_database_engine(f"sqlite:///{tmp_path / 'suite-api.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        setup_source(session)
        confirm(session, "history", ["hero"])

    def override() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def service(
    session: Session,
    *,
    required_style_origin: Literal["customer_upload"] | None = None,
) -> Any:
    from opc_style.services.style_suites import StyleSuiteService
    from opc_style.settings import StyleSuitePlanningSettings

    return StyleSuiteService(
        session,
        settings=StyleSuitePlanningSettings(
            candidate_estimate_usd=Decimal("0.12"),
            real_generation_enabled=False,
        ),
        required_style_origin=required_style_origin,
    )


def test_paid_boundary_rejects_legacy_empty_customer_style(session: Session) -> None:
    from opc_style.services.style_suites import StyleSuiteService
    from opc_style.settings import StyleSuitePlanningSettings

    source = setup_source(session)
    confirm(session, "legacy-empty-style", ["hero"])
    paid = StyleSuiteService(
        session,
        settings=StyleSuitePlanningSettings(
            candidate_estimate_usd=Decimal("0.12"),
            real_generation_enabled=True,
        ),
        enforce_real_pilot_boundary=True,
        required_style_origin="customer_upload",
    )

    with pytest.raises(DomainRuleError, match="style_suite_no_active_styles"):
        paid.preview(A, source.product_id, source.id)


def test_paid_pilot_ignores_unselected_ineligible_customer_style(
    session: Session,
) -> None:
    """An unrelated legacy style must not block the exact eligible paid pilot scope."""
    from opc_style.services.style_suites import StyleSuiteService
    from opc_style.settings import StyleSuitePlanningSettings

    source = setup_source(session)
    confirm(session, "aaa-legacy-style", ["hero"])
    samples = []
    for index in range(10):
        sample = StyleSample(
            customer_id=A,
            photo_id=f"real-pilot-history-{index}",
            authorized_at=datetime.now(UTC),
            consent_provenance="customer-consent-v2",
            consent_text_version="v1",
            content_sha256=f"{index + 100:064x}",
            mime_type="image/jpeg",
            width=100,
            height=100,
            asset_store_namespace="customer-upload",
        )
        session.add(sample)
        samples.append(sample)
    session.commit()
    evidence_ids = [sample.id for sample in samples]
    real_style = HistoricalStyleConfirmationService(session).confirm(
        A,
        HistoricalStyleGroupsConfirmRequest.model_validate(
            {
                "confirmation_key": "exact-real-pilot-style-v1",
                "confirmed": True,
                "groups": [
                    {
                        "name": "zzz-real-style",
                        "evidence_sample_ids": evidence_ids,
                        "analysis_mode": "real_visual_analysis_proposal",
                        "analyzer_version": "openai-style-analysis-v1",
                        "proposal_fingerprint": "f" * 64,
                        "reference_selection_version": "pixel-diversity-farthest-v1",
                        "reference_manifest_sha256": "e" * 64,
                        "palette": ["warm"],
                        "lighting": ["soft"],
                        "composition": ["centered"],
                        "backgrounds": ["plain"],
                        "field_evidence": {
                            field: evidence_ids
                            for field in (
                                "palette",
                                "lighting",
                                "composition",
                                "backgrounds",
                            )
                        },
                        "slots": [
                            {
                                **slot("hero"),
                                "rules": {
                                    **slot("hero")["rules"],
                                    "composition": ["centered"],
                                    "lighting": ["soft"],
                                },
                            }
                        ],
                    }
                ],
            }
        ),
    )[0]
    paid = StyleSuiteService(
        session,
        settings=StyleSuitePlanningSettings(
            candidate_estimate_usd=Decimal("0.12"),
            real_generation_enabled=True,
        ),
        enforce_real_pilot_boundary=True,
        required_style_origin="customer_upload",
    )

    plan = paid.preview(
        A,
        source.product_id,
        source.id,
        pilot_scope=StyleSuitePilotScope(
            scene_style_version_id=real_style.version.id,
            slot_key="hero",
        ),
    )

    assert [(item.style_name, item.slot_key) for item in plan.items] == [
        ("zzz-real-style", "hero")
    ]


def test_preview_endpoint_exists_without_provider_configuration(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/style-suites/preview",
        headers=HEADERS,
        json={"product_id": "product-001", "source_asset_id": str(uuid4())},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "style_suite_source_unavailable"


def test_preview_api_filters_legacy_empty_style(
    client: TestClient,
) -> None:
    source = client.get("/v1/product-assets/product-001", headers=HEADERS).json()[
        "assets"
    ][0]
    response = client.post(
        "/v1/style-suites/preview",
        headers=HEADERS,
        json={"product_id": "product-001", "source_asset_id": source["id"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "style_suite_no_active_styles"
    foreign = client.post(
        "/v1/style-suites/preview",
        headers={"X-Customer-Id": str(B)},
        json={"product_id": "product-001", "source_asset_id": source["id"]},
    )
    assert foreign.status_code == 409
    assert foreign.json()["error"]["code"] == "style_suite_source_unavailable"


def test_disabled_execution_api_persists_matrix_and_get_is_customer_scoped(
    client: TestClient,
) -> None:
    source = client.get("/v1/product-assets/product-001", headers=HEADERS).json()[
        "assets"
    ][0]
    created = client.post(
        "/v1/style-suites",
        headers=HEADERS,
        json={
            "product_id": "product-001",
            "source_asset_id": source["id"],
            "idempotency_key": "disabled-suite-api",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["state"] == "planned"
    assert created.json()["candidate_count"] == 0
    fetched = client.get(f"/v1/style-suites/{created.json()['id']}", headers=HEADERS)
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    foreign = client.get(
        f"/v1/style-suites/{created.json()['id']}",
        headers={"X-Customer-Id": str(B)},
    )
    assert foreign.status_code == 404


def test_preview_expands_all_owned_active_slots_in_stable_order_without_writes(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opc_style.domain.image_generator import DeterministicLocalImageGenerator
    from opc_style.providers.openai_images import OpenAIImageGenerator

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("preview attempted image generation")

    monkeypatch.setattr(DeterministicLocalImageGenerator, "generate", forbidden)
    monkeypatch.setattr(OpenAIImageGenerator, "generate", forbidden)
    source = setup_source(session)
    warm = confirm(session, "warm-table", ["wear", "hero", "detail"])
    clean = confirm(session, "clean-white", ["side", "hero"])
    confirm(session, "foreign", ["hero"], B)
    inactive = confirm(session, "inactive", ["hero"])
    HistoricalStyleConfirmationService(session).deactivate(A, inactive.scene.id)
    before = session.scalar(select(func.count()).select_from(StyleCard))
    plan = service(session).preview(A, source.product_id, source.id)
    assert [(i.style_name, i.slot_key) for i in plan.items] == [
        ("clean-white", "hero"),
        ("clean-white", "side"),
        ("warm-table", "detail"),
        ("warm-table", "hero"),
        ("warm-table", "wear"),
    ]
    assert (
        plan.style_count,
        plan.slot_count,
        plan.child_job_count,
        plan.candidate_count,
    ) == (2, 5, 5, 15)
    assert plan.maximum_estimated_cost_usd == Decimal("1.80")
    assert plan.candidate_estimate_usd == Decimal("0.12")
    assert plan.cost_basis == "local_planning_estimate"
    assert plan.source_content_sha256 == "a" * 64
    assert {i.scene_style_version_id for i in plan.items} == {
        clean.version.id,
        warm.version.id,
    }
    assert all(
        i.style_snapshot["main_style_card_version_id"] is None for i in plan.items
    )
    assert all(i.slot_snapshot["rules"]["background"] == ["white"] for i in plan.items)
    assert service(session).preview(A, source.product_id, source.id) == plan
    assert session.scalar(select(func.count()).select_from(StyleCard)) == before
    for table in ("style_suite_runs", "style_suite_run_items", "image_output_jobs"):
        assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    assert not session.new and not session.dirty


def test_pilot_preview_selects_exactly_one_owned_style_slot(session: Session) -> None:
    """A paid pilot cannot accidentally expand into the complete style matrix."""
    source = setup_source(session)
    selected = confirm(session, "warm-table", ["hero", "detail"])
    confirm(session, "clean-white", ["hero"])

    result = service(session).preview(
        A,
        source.product_id,
        source.id,
        pilot_scope=StyleSuitePilotScope(
            scene_style_version_id=selected.version.id,
            slot_key="hero",
        ),
    )

    assert result.style_count == 1
    assert result.slot_count == 1
    assert result.child_job_count == 1
    assert result.candidate_count == 3
    assert result.maximum_estimated_cost_usd == Decimal("0.36")
    assert [(item.scene_style_version_id, item.slot_key) for item in result.items] == [
        (selected.version.id, "hero")
    ]


@pytest.mark.parametrize("reason", ["foreign_style", "unknown_slot"])
def test_pilot_scope_rejects_foreign_style_or_unknown_slot(
    session: Session, reason: str
) -> None:
    """Changing the requested style or slot must fail closed before planning."""
    source = setup_source(session)
    owned = confirm(session, "owned", ["hero"])
    foreign = confirm(session, "foreign", ["hero"], B)
    scope = StyleSuitePilotScope(
        scene_style_version_id=(
            foreign.version.id if reason == "foreign_style" else owned.version.id
        ),
        slot_key="hero" if reason == "foreign_style" else "unknown",
    )

    with pytest.raises(DomainRuleError, match="style_suite_pilot_scope_unavailable"):
        service(session).preview(A, source.product_id, source.id, pilot_scope=scope)


def test_new_plan_uses_only_latest_version_and_old_plan_keeps_snapshots(
    session: Session,
) -> None:
    source = setup_source(session)
    original = confirm(session, "history", ["hero", "side"])
    old_plan = service(session).preview(A, source.product_id, source.id)
    edited = HistoricalStyleConfirmationService(session).edit_scene(
        A,
        original.scene.id,
        {
            "tone": ["cool"],
            "slots": [slot("detail"), slot("hero", False)],
        },
    )
    new_plan = service(session).preview(A, source.product_id, source.id)
    assert [item.slot_key for item in new_plan.items] == ["detail"]
    assert new_plan.items[0].scene_style_version_id == edited.version.id
    assert [item.slot_key for item in old_plan.items] == ["hero", "side"]
    assert old_plan.items[0].style_snapshot["overrides"]["tone"] == ["warm"]


@pytest.mark.parametrize(
    "reason",
    [
        "missing",
        "foreign",
        "wrong_product",
        "unconfirmed",
        "denied",
        "unknown",
        "derived",
    ],
)
def test_source_gates_fail_before_planning(session: Session, reason: str) -> None:
    source = setup_source(
        session,
        permission=reason != "unknown",
        **(
            {"confirmed": False}
            if reason == "unconfirmed"
            else {"identity": "derived_real_photo"}
            if reason == "derived"
            else {}
        ),
    )
    confirm(session, "history", ["hero"])
    if reason == "denied":
        session.add(
            ProductAssetPermissionEvent(
                customer_id=A,
                product_asset_id=source.id,
                transform_permission=False,
                occurred_at=datetime.now(UTC) + timedelta(days=1),
                provenance="withdrawal",
            )
        )
        session.commit()
    with pytest.raises(DomainRuleError):
        service(session).preview(
            B if reason == "foreign" else A,
            "wrong" if reason == "wrong_product" else source.product_id,
            uuid4() if reason == "missing" else source.id,
        )


@pytest.mark.parametrize("reason", ["none", "withdrawn", "inactive", "legacy"])
def test_missing_active_style_or_current_evidence_permission_fails(
    session: Session, reason: str
) -> None:
    source = setup_source(session)
    if reason in {"withdrawn", "inactive"}:
        history = confirm(session, "history", ["hero"])
        if reason == "inactive":
            HistoricalStyleConfirmationService(session).deactivate(A, history.scene.id)
        else:
            sample = session.scalar(
                select(StyleSample).where(StyleSample.customer_id == A)
            )
            assert sample is not None
            sample.withdrawn_at = datetime.now(UTC)
            session.commit()
    elif reason == "legacy":
        session.add(SceneStyle(customer_id=A, name="legacy", rules={}))
        session.commit()
    with pytest.raises(DomainRuleError):
        service(session).preview(A, source.product_id, source.id)


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("demo", "style_suite_no_active_styles"),
        ("legacy_missing_origin", "style_suite_no_active_styles"),
        ("withdrawn", "style_suite_style_permission_unavailable"),
        ("missing_content", "style_suite_style_permission_unavailable"),
    ],
)
def test_paid_style_origin_negative_matrix(
    session: Session, case: str, expected_code: str
) -> None:
    """Only intact customer-upload evidence may reach a paid suite preview."""
    source = setup_source(session)
    if case == "demo":
        confirm(
            session,
            "demo",
            ["hero"],
            consent_provenance="local_synthetic_acceptance",
            style_origin="local_demo",
        )
    else:
        confirmed = confirm(session, "customer-style", ["hero"])
        if case == "legacy_missing_origin":
            legacy_content = dict(confirmed.version.content)
            legacy_content.pop("style_origin")
            session.execute(
                update(StyleCardVersion)
                .where(StyleCardVersion.id == confirmed.version.id)
                .values(content=legacy_content)
            )
            session.commit()
            session.expire_all()
        else:
            sample = session.scalar(
                select(StyleSample).where(StyleSample.customer_id == A)
            )
            assert sample is not None
            if case == "withdrawn":
                sample.withdrawn_at = datetime.now(UTC)
            else:
                sample.consent_text_version = None
                sample.content_sha256 = None
                sample.mime_type = None
                sample.width = None
                sample.height = None
                sample.asset_store_namespace = None
            session.commit()

    with pytest.raises(DomainRuleError) as caught:
        service(session, required_style_origin="customer_upload").preview(
            A, source.product_id, source.id
        )

    assert caught.value.code == expected_code


def test_renamed_customer_style_remains_paid_eligible_with_a_new_version(
    session: Session,
) -> None:
    """Renaming appends a version while retaining the sealed customer origin."""
    source = setup_source(session)
    original = confirm(session, "old-name", ["hero"])
    renamed = HistoricalStyleConfirmationService(session).edit_scene(
        A, original.scene.id, {"name": "我的暖光风格"}
    )

    plan = service(session, required_style_origin="customer_upload").preview(
        A, source.product_id, source.id
    )

    assert renamed.version.id != original.version.id
    assert renamed.version.content["style_origin"] == "customer_upload"
    assert [(item.style_name, item.scene_style_version_id) for item in plan.items] == [
        ("我的暖光风格", renamed.version.id)
    ]


@pytest.mark.parametrize(
    "amount,enabled,valid",
    [
        ("-0.01", False, False),
        ("0", True, False),
        ("0", False, True),
        ("0.12", True, True),
    ],
)
def test_planning_estimate_is_nonnegative_and_not_free_real_generation(
    amount: str, enabled: bool, valid: bool
) -> None:
    from pydantic import ValidationError

    from opc_style.settings import StyleSuitePlanningSettings

    if valid:
        settings = StyleSuitePlanningSettings(
            candidate_estimate_usd=amount, real_generation_enabled=enabled
        )
        assert settings.candidate_estimate_usd == Decimal(amount)
    else:
        with pytest.raises(ValidationError):
            StyleSuitePlanningSettings(
                candidate_estimate_usd=amount, real_generation_enabled=enabled
            )


def persist_plan(session: Session) -> tuple[Any, Any]:
    from opc_style.models import StyleSuiteRun, StyleSuiteRunItem

    source = setup_source(session)
    confirm(session, "history", ["hero"])
    plan = service(session).preview(A, source.product_id, source.id)
    run = StyleSuiteRun(**plan.model_dump(exclude={"items"}))
    session.add(run)
    session.flush()
    item = StyleSuiteRunItem(customer_id=A, run_id=run.id, **plan.items[0].model_dump())
    session.add(item)
    session.commit()
    return run, item


@pytest.mark.parametrize(
    "field", ["source_content_sha256", "candidate_estimate_usd", "candidate_count"]
)
def test_run_facts_are_immutable_but_execution_state_can_advance(
    session: Session, field: str
) -> None:
    run, _ = persist_plan(session)
    run.state = "running"
    session.commit()
    setattr(
        run,
        field,
        {
            "source_content_sha256": "c" * 64,
            "candidate_estimate_usd": Decimal("0.50"),
            "candidate_count": 99,
        }[field],
    )
    with pytest.raises(DomainRuleError, match="style_suite_run_immutable"):
        session.commit()
    session.rollback()


def test_run_item_snapshots_and_pins_cannot_be_rewritten(session: Session) -> None:
    _, item = persist_plan(session)
    item.slot_snapshot = {"slot_key": "forged"}
    with pytest.raises(DomainRuleError, match="style_suite_run_item_immutable"):
        session.commit()
    session.rollback()


def test_database_rejects_legacy_style_item_inside_real_paid_run(
    session: Session,
) -> None:
    from opc_style.models import StyleSuiteRun, StyleSuiteRunItem

    source = setup_source(session)
    confirm(session, "legacy-empty-style", ["hero"])
    plan = service(session).preview(A, source.product_id, source.id)
    run_facts = plan.model_dump(exclude={"items"})
    run_facts.update(
        real_generation_enabled=True,
        pilot_scope={
            "scene_style_version_id": str(plan.items[0].scene_style_version_id),
            "slot_key": plan.items[0].slot_key,
        },
    )
    run = StyleSuiteRun(**run_facts)
    session.add(run)
    session.flush()
    session.add(
        StyleSuiteRunItem(
            customer_id=A,
            run_id=run.id,
            **plan.items[0].model_dump(),
        )
    )

    with pytest.raises(DomainRuleError, match="style_suite_real_style_ineligible"):
        session.commit()
    session.rollback()


def test_migrated_database_seals_matrix_and_blocks_duplicate_and_foreign_items(
    migrated_session: Session,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from opc_style.models import StyleSuiteRunItem

    session = migrated_session
    run, item = persist_plan(session)
    for statement in (
        "UPDATE style_suite_runs SET candidate_count = 99",
        "UPDATE style_suite_runs SET idempotency_key = 'forged'",
        "UPDATE style_suite_run_items SET slot_snapshot = '{}'",
        "DELETE FROM style_suite_runs",
        "DELETE FROM style_suite_run_items",
    ):
        with pytest.raises(IntegrityError):
            session.execute(text(statement))
        session.rollback()
    values = {
        column.name: getattr(item, column.name)
        for column in StyleSuiteRunItem.__table__.columns
    }
    values["id"] = uuid4()
    with pytest.raises(IntegrityError):
        session.execute(StyleSuiteRunItem.__table__.insert().values(**values))
    session.rollback()
    values.update(id=uuid4(), customer_id=B)
    with pytest.raises(IntegrityError):
        session.execute(StyleSuiteRunItem.__table__.insert().values(**values))
    session.rollback()
    assert session.get(type(run), run.id).candidate_count == 3


def test_migrated_database_allows_one_exact_child_binding_then_blocks_rebinding(
    migrated_session: Session,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from opc_style.models import ImageOutputJob, ProductAssetLink

    session = migrated_session
    run, item = persist_plan(session)
    key = (
        f"suite:{run.id}:{item.scene_style_version_id}:"
        f"{item.slot_key}:{run.source_content_sha256}"
    )

    def job(job_key: str) -> ImageOutputJob:
        row = ImageOutputJob(
            customer_id=A,
            output_type="product_style",
            style_card_version_id=item.scene_style_version_id,
            scene_style_id=item.scene_style_id,
            output_ratio="4:5",
            adapter_version="test",
            status="queued",
            request_metadata={
                "slot_key": item.slot_key,
                "sources": [
                    {
                        "product_id": run.product_id,
                        "asset_id": str(run.source_asset_id),
                    }
                ],
            },
            idempotency_key=job_key,
        )
        session.add(row)
        session.flush()
        session.add(
            ProductAssetLink(
                customer_id=A,
                product_asset_id=run.source_asset_id,
                output_job_id=row.id,
                position=1,
            )
        )
        session.commit()
        return row

    exact = job(key)
    session.execute(
        text("UPDATE style_suite_run_items SET output_job_id=:job WHERE id=:item"),
        {"job": str(exact.id), "item": str(item.id)},
    )
    session.commit()
    with pytest.raises(IntegrityError, match="style_suite_item_job_mismatch"):
        session.execute(
            text("UPDATE style_suite_run_items SET output_job_id=NULL WHERE id=:item"),
            {"item": str(item.id)},
        )
    session.rollback()
    wrong = job("wrong-child-key")
    with pytest.raises(IntegrityError, match="style_suite_item_job_mismatch"):
        session.execute(
            text("UPDATE style_suite_run_items SET output_job_id=:job WHERE id=:item"),
            {"job": str(wrong.id), "item": str(item.id)},
        )
    session.rollback()


def test_migrated_database_rejects_blank_suite_parent_key(
    migrated_session: Session,
) -> None:
    from sqlalchemy.exc import IntegrityError

    session = migrated_session
    run, _ = persist_plan(session)
    values = {
        column.name: getattr(run, column.name) for column in type(run).__table__.columns
    }
    values.update(id=uuid4(), idempotency_key="   ")
    with pytest.raises(IntegrityError, match="style_suite_run_key_blank"):
        session.execute(type(run).__table__.insert().values(**values))
    session.rollback()


def test_migrated_run_rejects_appending_a_new_version_beyond_sealed_budget(
    migrated_session: Session,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from opc_style.models import StyleSuiteRunItem

    session = migrated_session
    run, _ = persist_plan(session)
    confirm(session, "second-history", ["hero"])
    extra = service(session).preview(A, run.product_id, run.source_asset_id).items[1]
    with pytest.raises(IntegrityError, match="style_suite_run_item_limit"):
        session.execute(
            StyleSuiteRunItem.__table__.insert().values(
                id=uuid4(),
                customer_id=A,
                run_id=run.id,
                **extra.model_dump(),
                state="planned",
                created_at=datetime.now(UTC),
            )
        )
    session.rollback()
