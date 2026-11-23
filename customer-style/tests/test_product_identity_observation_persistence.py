"""Persistence guards for reusable product identity observations."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    ProductAsset,
    ProductIdentityAnalysisRun,
    ProductIdentityCardVersion,
    ProductIdentityObservationReview,
    ProductIdentityObservationRun,
    StyleSuiteRun,
)

CUSTOMER_A = UUID("00000000-0000-0000-0000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-0000-0000-0000000000b2")
NOW = datetime(2026, 9, 28, tzinfo=UTC)


@pytest.fixture
def identity_graph(session: Session, customer_factory):
    customer_factory(CUSTOMER_A)
    source = ProductAsset(
        customer_id=CUSTOMER_A,
        product_id="product-a",
        photo_id="photo-a-source",
        identity="original_real_photo",
        confirmed=True,
        source_type="classification_handoff",
        quality_flags=[],
        occurred_at=NOW,
        content_sha256="a" * 64,
        mime_type="image/png",
        width=10,
        height=10,
        storage_path="source.png",
    )
    target = ProductAsset(
        customer_id=CUSTOMER_A,
        product_id="product-a",
        photo_id="photo-a-target",
        identity="original_real_photo",
        confirmed=True,
        source_type="classification_handoff",
        quality_flags=[],
        occurred_at=NOW,
        content_sha256="b" * 64,
        mime_type="image/png",
        width=10,
        height=10,
        storage_path="target.png",
    )
    session.add_all([source, target])
    session.flush()
    analysis = ProductIdentityAnalysisRun(
        customer_id=CUSTOMER_A,
        product_asset_id=source.id,
        product_id="product-a",
        source_content_sha256=source.content_sha256,
        idempotency_key="identity-source",
        model="controlled-model",
        authorized_usd=Decimal("0.10"),
        input_manifest_sha256="c" * 64,
        input_manifest={},
        dispatch_state="provider_responded",
        outbound_attempts=1,
        proposal={},
    )
    session.add(analysis)
    session.flush()
    card = ProductIdentityCardVersion(
        customer_id=CUSTOMER_A,
        product_asset_id=source.id,
        product_id="product-a",
        analysis_run_id=analysis.id,
        version=1,
        source_content_sha256=source.content_sha256,
        card_payload={"category": {"value": "bracelet", "certainty": "observed"}},
        card_sha256="d" * 64,
    )
    session.add(card)
    session.flush()
    return source, target, card


def _observation(target: ProductAsset, card: ProductIdentityCardVersion, **values):
    defaults = {
        "customer_id": CUSTOMER_A,
        "product_asset_id": target.id,
        "product_id": target.product_id,
        "source_content_sha256": target.content_sha256,
        "product_identity_card_version_id": card.id,
        "product_identity_card_sha256": card.card_sha256,
        "idempotency_key": "observe-target",
        "observer": "controlled-model",
        "observer_version": "observer-v1",
        "observation_mode": "real_product_identity_proposal",
        "policy_version": "product-identity-gate-v1",
        "authorized_usd": Decimal("0.10"),
        "input_manifest_sha256": "e" * 64,
        "input_manifest": {"image_count": 1},
        "dispatch_state": "provider_responded",
        "outbound_attempts": 1,
        "observation_payload": {"category": "bracelet"},
        "decision": "needs_review",
        "reason_codes": ["visibility_limited"],
        "result_sha256": "f" * 64,
    }
    defaults.update(values)
    return ProductIdentityObservationRun(**defaults)


def test_card_from_first_photo_can_observe_second_photo_of_same_product(
    session: Session, identity_graph
) -> None:
    _, target, card = identity_graph
    observation = _observation(target, card)

    session.add(observation)
    session.flush()

    assert observation.product_asset_id == target.id
    assert observation.product_identity_card_version_id == card.id


def test_reserved_observation_persists_optional_json_as_sql_null(
    session: Session, identity_graph
) -> None:
    _, target, card = identity_graph
    observation = _observation(
        target,
        card,
        dispatch_state="reserved",
        outbound_attempts=0,
        observation_payload=None,
        decision=None,
        reason_codes=None,
        result_sha256=None,
    )
    session.add(observation)
    session.flush()

    row = session.execute(
        text(
            "SELECT observation_payload IS NULL, reason_codes IS NULL "
            "FROM product_identity_observation_runs WHERE id=:id"
        ),
        {"id": str(observation.id)},
    ).one()

    assert row == (1, 1)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("customer_id", CUSTOMER_B),
        ("product_id", "product-b"),
        ("source_content_sha256", "1" * 64),
        ("product_identity_card_sha256", "2" * 64),
    ),
)
def test_observation_rejects_cross_scope_lineage(
    session: Session, customer_factory, identity_graph, field: str, value: object
) -> None:
    _, target, card = identity_graph
    if field == "customer_id":
        customer_factory(CUSTOMER_B)
    session.add(_observation(target, card, **{field: value}))

    with pytest.raises(IntegrityError):
        session.flush()


def test_observation_key_is_unique_per_customer(
    session: Session, identity_graph
) -> None:
    _, target, card = identity_graph
    session.add(_observation(target, card))
    session.flush()
    session.add(_observation(target, card))

    with pytest.raises(IntegrityError):
        session.flush()


def test_observation_payload_and_decision_are_immutable(
    session: Session, identity_graph
) -> None:
    _, target, card = identity_graph
    observation = _observation(target, card)
    session.add(observation)
    session.flush()
    observation.observation_payload["category"] = "necklace"
    flag_modified(observation, "observation_payload")

    with pytest.raises(DomainRuleError, match="product_identity_observation_immutable"):
        session.flush()


def test_review_is_append_only_and_only_for_nonpassing_observation(
    session: Session, identity_graph
) -> None:
    _, target, card = identity_graph
    observation = _observation(target, card)
    session.add(observation)
    session.flush()
    review = ProductIdentityObservationReview(
        customer_id=CUSTOMER_A,
        observation_run_id=observation.id,
        product_asset_id=target.id,
        product_id=target.product_id,
        source_content_sha256=target.content_sha256,
        product_identity_card_version_id=card.id,
        product_identity_card_sha256=card.card_sha256,
        observation_result_sha256=observation.result_sha256,
        decision="approved",
        reason="扣头被手腕遮挡，已人工核对原商品",
    )
    session.add(review)
    session.flush()
    review.reason = "changed"

    with pytest.raises(DomainRuleError, match="product_identity_observation_review_immutable"):
        session.flush()


def test_passing_observation_cannot_receive_review(
    session: Session, identity_graph
) -> None:
    _, target, card = identity_graph
    observation = _observation(
        target,
        card,
        decision="pass",
        reason_codes=[],
    )
    session.add(observation)
    session.flush()
    session.add(
        ProductIdentityObservationReview(
            customer_id=CUSTOMER_A,
            observation_run_id=observation.id,
            product_asset_id=target.id,
            product_id=target.product_id,
            source_content_sha256=target.content_sha256,
            product_identity_card_version_id=card.id,
            product_identity_card_sha256=card.card_sha256,
            observation_result_sha256=observation.result_sha256,
            decision="approved",
            reason="不应审核已通过记录",
        )
    )

    with pytest.raises(DomainRuleError, match="product_identity_observation_review_invalid"):
        session.flush()


def test_style_suite_observation_binding_is_all_or_none(
    session: Session, identity_graph
) -> None:
    _, target, card = identity_graph
    suite = StyleSuiteRun(
        customer_id=CUSTOMER_A,
        product_id=target.product_id,
        source_asset_id=target.id,
        source_content_sha256=target.content_sha256,
        product_identity_card_version_id=card.id,
        product_identity_card_sha256=card.card_sha256,
        product_identity_observation_run_id=UUID(
            "10000000-0000-4000-8000-000000000001"
        ),
        product_identity_observation_result_sha256=None,
        style_count=1,
        slot_count=1,
        child_job_count=1,
        candidate_count=3,
        candidate_estimate_usd=Decimal("0.10"),
        maximum_estimated_cost_usd=Decimal("0.30"),
        cost_basis="local_planning_estimate",
        currency="USD",
        real_generation_enabled=True,
        idempotency_key="suite-partial-observation",
    )
    session.add(suite)

    with pytest.raises(IntegrityError):
        session.flush()
