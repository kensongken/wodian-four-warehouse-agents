"""Single-dispatch observation of new photos against a reusable identity card."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_identity import (
    ProductIdentityImage,
    identity_card_payload,
    identity_card_sha256,
    normalize_identity_draft,
)
from opc_style.models import (
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductIdentityAnalysisRun,
    ProductIdentityCardVersion,
    ProductIdentityObservationRun,
)
from opc_style.services.product_identity_observations import (
    ProductIdentityObservationService,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER = UUID("00000000-0000-0000-0000-0000000000a1")
NOW = datetime(2026, 9, 28, tzinfo=UTC)
MAXIMUM_USD = Decimal("0.125000")
SOURCE_BYTES = b"identity-source-photo"
TARGET_BYTES = b"same-product-new-photo"


def _identity_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "category": {"value": "bracelet", "certainty": "observed"},
        "primary_colors": {"value": ["red"], "certainty": "observed"},
        "visible_surface_appearance": {
            "value": ["glossy"],
            "certainty": "observed",
        },
        "component_shapes": {"value": ["round_bead"], "certainty": "observed"},
        "visible_component_count": {"value": 18, "certainty": "observed"},
        "component_arrangement": {
            "value": "single_strand",
            "certainty": "observed",
        },
        "core_accessories": {
            "value": [{"kind": "clasp", "visible_count": 1}],
            "certainty": "observed",
        },
        "overall_form": {"value": "closed_loop", "certainty": "observed"},
        "relative_proportions": {"value": "珠体大小接近", "certainty": "observed"},
        "distinguishing_marks": {"value": "无明显标记", "certainty": "observed"},
        "visibility_limits": {"value": [], "certainty": "observed"},
    }
    payload.update(overrides)
    return payload


class RecordingObserver:
    analyzer_version = "observer-v1"
    analysis_mode = "real_product_identity_proposal"

    def __init__(
        self,
        *,
        failure: bool = False,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.calls = 0
        self.images: list[ProductIdentityImage] = []
        self.failure = failure
        self.payload = payload or _identity_payload()

    def analyze(self, image: ProductIdentityImage):
        self.calls += 1
        self.images.append(image)
        if self.failure:
            raise RuntimeError("provider body must not escape")
        return normalize_identity_draft(self.payload)


@pytest.fixture
def graph(session: Session, customer_factory, tmp_path):
    customer_factory(CUSTOMER)
    source_sha = sha256(SOURCE_BYTES).hexdigest()
    target_sha = sha256(TARGET_BYTES).hexdigest()
    source = ProductAsset(
        customer_id=CUSTOMER,
        product_id="product-a",
        photo_id="photo-source",
        identity="original_real_photo",
        confirmed=True,
        source_type="classification_handoff",
        quality_flags=[],
        occurred_at=NOW,
        content_sha256=source_sha,
        mime_type="image/png",
        width=32,
        height=32,
    )
    target = ProductAsset(
        customer_id=CUSTOMER,
        product_id="product-a",
        photo_id="photo-target",
        identity="original_real_photo",
        confirmed=True,
        source_type="classification_handoff",
        quality_flags=[],
        occurred_at=NOW,
        content_sha256=target_sha,
        mime_type="image/png",
        width=32,
        height=32,
    )
    session.add_all([source, target])
    session.flush()
    session.add_all(
        [
            ProductAssetPermissionEvent(
                customer_id=CUSTOMER,
                product_asset_id=asset.id,
                transform_permission=True,
                occurred_at=NOW,
                provenance="product_asset_intake",
            )
            for asset in (source, target)
        ]
    )
    run = ProductIdentityAnalysisRun(
        customer_id=CUSTOMER,
        product_asset_id=source.id,
        product_id=source.product_id,
        source_content_sha256=source_sha,
        idempotency_key="identity-source",
        model="identity-model",
        authorized_usd=Decimal("0.1"),
        input_manifest_sha256="1" * 64,
        input_manifest={},
        dispatch_state="provider_responded",
        outbound_attempts=1,
        proposal=_identity_payload(),
    )
    session.add(run)
    session.flush()
    draft = normalize_identity_draft(_identity_payload())
    card = ProductIdentityCardVersion(
        customer_id=CUSTOMER,
        product_asset_id=source.id,
        product_id=source.product_id,
        analysis_run_id=run.id,
        version=1,
        source_content_sha256=source_sha,
        card_payload=identity_card_payload(draft),
        card_sha256=identity_card_sha256(draft),
    )
    session.add(card)
    session.commit()
    store = LocalSyntheticAssetStore(tmp_path / "observation-store")
    store.persist_original(CUSTOMER, source.id, SOURCE_BYTES, source_sha)
    store.persist_original(CUSTOMER, target.id, TARGET_BYTES, target_sha)
    yield source, target, card, store
    store.close()


def _service(session: Session, store, observer: RecordingObserver):
    return ProductIdentityObservationService(
        session,
        content_store=store,
        analyzer=observer,
        model="observation-model",
        maximum_usd=MAXIMUM_USD,
    )


def test_card_from_photo_a_passes_same_product_photo_b_and_replays(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    observer = RecordingObserver()
    service = _service(session, store, observer)
    preview = service.preview(CUSTOMER, target.id, card.id)

    first = service.execute(
        CUSTOMER,
        target.id,
        card.id,
        idempotency_key="observe-target",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )
    replay = service.execute(
        CUSTOMER,
        target.id,
        card.id,
        idempotency_key="observe-target",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )

    assert preview.source_content_sha256 == target.content_sha256
    assert preview.product_identity_card_sha256 == card.card_sha256
    assert first.decision == "pass"
    assert first.reason_codes == ()
    assert replay == first
    assert replay.created is False
    assert observer.calls == 1
    assert service.current(CUSTOMER, target.id, card.id) == replay


def test_card_source_photo_is_verified_locally_without_provider_dispatch(
    session: Session, graph
) -> None:
    source, _, card, store = graph
    observer = RecordingObserver(
        payload=_identity_payload(
            visible_component_count={"value": 999, "certainty": "observed"},
            component_arrangement={
                "value": "continuous_chain",
                "certainty": "observed",
            },
        )
    )
    service = _service(session, store, observer)
    preview = service.preview(CUSTOMER, source.id, card.id)

    outcome = service.execute(
        CUSTOMER,
        source.id,
        card.id,
        idempotency_key="observe-card-source-locally",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )
    run = session.get(ProductIdentityObservationRun, outcome.run_id)

    assert preview.maximum_usd == Decimal("0.000000")
    assert preview.input_image_count == 0
    assert outcome.decision == "pass"
    assert outcome.reason_codes == ("card_source_exact_match",)
    assert identity_card_payload(outcome.observation) == card.card_payload
    assert observer.calls == 0
    assert run is not None
    assert run.authorized_usd == Decimal("0.000000")
    assert run.dispatch_state == "locally_verified"
    assert run.outbound_attempts == 0
    assert service.current(CUSTOMER, source.id, card.id) == outcome
    evidence = service.validate_generation_evidence(
        CUSTOMER,
        source.product_id,
        source.id,
        card.id,
        outcome.run_id,
    )
    assert evidence.observation_run_id == outcome.run_id
    assert evidence.review_id is None


def test_wrong_cost_or_manifest_never_dispatches(session: Session, graph) -> None:
    _, target, card, store = graph
    observer = RecordingObserver()
    service = _service(session, store, observer)
    preview = service.preview(CUSTOMER, target.id, card.id)

    with pytest.raises(
        DomainRuleError, match="product_identity_observation_confirmation_required"
    ):
        service.execute(
            CUSTOMER,
            target.id,
            card.id,
            idempotency_key="wrong-confirmation",
            confirmed_maximum_usd=Decimal("0.01"),
            confirmed_manifest_sha256=preview.manifest_sha256,
        )

    assert observer.calls == 0


@pytest.mark.parametrize(
    ("payload", "decision", "reason"),
    [
        (
            _identity_payload(
                core_accessories={"value": [], "certainty": "observed"},
                visibility_limits={"value": ["clasp_hidden"], "certainty": "observed"},
            ),
            "needs_review",
            "visibility_limited",
        ),
        (
            _identity_payload(
                core_accessories={
                    "value": [
                        {"kind": "clasp", "visible_count": 1},
                        {"kind": "pendant", "visible_count": 1},
                    ],
                    "certainty": "observed",
                }
            ),
            "block",
            "core_accessory_extra",
        ),
    ],
)
def test_review_and_block_outcomes_are_persisted_without_provider_authority(
    session: Session,
    graph,
    payload: dict[str, object],
    decision: str,
    reason: str,
) -> None:
    _, target, card, store = graph
    observer = RecordingObserver(payload=payload)
    service = _service(session, store, observer)
    preview = service.preview(CUSTOMER, target.id, card.id)

    result = service.execute(
        CUSTOMER,
        target.id,
        card.id,
        idempotency_key=f"observe-{decision}",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )

    assert result.decision == decision
    assert result.reason_codes == (reason,)
    assert service.current(CUSTOMER, target.id, card.id) == result
    assert observer.calls == 1


def test_disabled_observer_is_rejected_before_preview(session: Session, graph) -> None:
    _, _, _, store = graph
    observer = RecordingObserver()
    observer.analysis_mode = "local_synthetic_proposal"

    with pytest.raises(
        DomainRuleError, match="product_identity_observation_provider_disabled"
    ):
        _service(session, store, observer)


def test_key_reuse_for_another_card_scope_is_rejected_without_dispatch(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    observer = RecordingObserver()
    service = _service(session, store, observer)
    preview = service.preview(CUSTOMER, target.id, card.id)
    service.execute(
        CUSTOMER,
        target.id,
        card.id,
        idempotency_key="fixed-key",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )

    with pytest.raises(DomainRuleError, match="product_identity_observation_scope_mismatch"):
        service.execute(
            CUSTOMER,
            target.id,
            UUID("10000000-0000-4000-8000-000000000099"),
            idempotency_key="fixed-key",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.manifest_sha256,
        )

    assert observer.calls == 1


def test_provider_failure_is_uncertain_and_same_key_never_retries(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    observer = RecordingObserver(failure=True)
    service = _service(session, store, observer)
    preview = service.preview(CUSTOMER, target.id, card.id)

    for _ in range(2):
        with pytest.raises(
            DomainRuleError, match="product_identity_observation_outcome_uncertain"
        ):
            service.execute(
                CUSTOMER,
                target.id,
                card.id,
                idempotency_key="uncertain-key",
                confirmed_maximum_usd=preview.maximum_usd,
                confirmed_manifest_sha256=preview.manifest_sha256,
            )

    assert observer.calls == 1


def test_reserved_zero_attempt_run_resumes_without_duplicate_dispatch(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    observer = RecordingObserver()
    service = _service(session, store, observer)
    preview = service.preview(CUSTOMER, target.id, card.id)
    prepared = service._prepare(CUSTOMER, target.id, card.id)
    reserved = ProductIdentityObservationRun(
        customer_id=CUSTOMER,
        product_asset_id=target.id,
        product_id=target.product_id,
        source_content_sha256=target.content_sha256,
        product_identity_card_version_id=card.id,
        product_identity_card_sha256=card.card_sha256,
        idempotency_key="resume-reserved-zero-attempt",
        observer=preview.model,
        observer_version=preview.observer_version,
        observation_mode="real_product_identity_proposal",
        policy_version=preview.policy_version,
        authorized_usd=preview.maximum_usd,
        input_manifest_sha256=preview.manifest_sha256,
        input_manifest=prepared.manifest,
        dispatch_state="reserved",
        outbound_attempts=0,
    )
    session.add(reserved)
    session.commit()

    outcome = service.execute(
        CUSTOMER,
        target.id,
        card.id,
        idempotency_key="resume-reserved-zero-attempt",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )

    session.refresh(reserved)
    assert outcome.decision == "pass"
    assert observer.calls == 1
    assert reserved.dispatch_state == "provider_responded"
    assert reserved.outbound_attempts == 1


def test_latest_permission_withdrawal_invalidates_previous_observation(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    observer = RecordingObserver()
    service = _service(session, store, observer)
    preview = service.preview(CUSTOMER, target.id, card.id)
    service.execute(
        CUSTOMER,
        target.id,
        card.id,
        idempotency_key="withdrawn-target",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )
    session.add(
        ProductAssetPermissionEvent(
            customer_id=CUSTOMER,
            product_asset_id=target.id,
            transform_permission=False,
            occurred_at=NOW + timedelta(seconds=1),
            provenance="customer_permission_withdrawal",
        )
    )
    session.commit()

    assert service.current(CUSTOMER, target.id, card.id) is None
    with pytest.raises(DomainRuleError, match="transform_permission_denied"):
        service.preview(CUSTOMER, target.id, card.id)


def _execute_observation(
    service: ProductIdentityObservationService,
    target: ProductAsset,
    card: ProductIdentityCardVersion,
    *,
    key: str,
):
    preview = service.preview(CUSTOMER, target.id, card.id)
    return service.execute(
        CUSTOMER,
        target.id,
        card.id,
        idempotency_key=key,
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )


def test_needs_review_approval_enables_evidence_without_mutating_card(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    observer = RecordingObserver(
        payload=_identity_payload(
            core_accessories={"value": [], "certainty": "observed"},
            visibility_limits={"value": ["clasp_hidden"], "certainty": "observed"},
        )
    )
    service = _service(session, store, observer)
    observation = _execute_observation(
        service, target, card, key="review-approval"
    )
    card_count = session.scalar(select(func.count()).select_from(ProductIdentityCardVersion))
    card_sha = card.card_sha256

    with pytest.raises(
        DomainRuleError, match="product_identity_observation_review_required"
    ):
        service.validate_generation_evidence(
            CUSTOMER,
            target.product_id,
            target.id,
            card.id,
            observation.run_id,
        )

    review = service.review(
        CUSTOMER,
        observation.run_id,
        decision="approved",
        reason="扣头被手腕遮挡，已人工核对原商品",
        confirmed=True,
    )
    evidence = service.validate_generation_evidence(
        CUSTOMER,
        target.product_id,
        target.id,
        card.id,
        observation.run_id,
        review_id=review.review_id,
    )

    assert review.created is True
    assert evidence.observation_run_id == observation.run_id
    assert evidence.observation_result_sha256 == observation.result_sha256
    assert evidence.review_id == review.review_id
    assert session.scalar(select(func.count()).select_from(ProductIdentityCardVersion)) == card_count
    assert session.get(ProductIdentityCardVersion, card.id).card_sha256 == card_sha


def test_identical_review_replays_but_conflicting_review_is_rejected(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    observer = RecordingObserver(
        payload=_identity_payload(category={"value": None, "certainty": "unclear"})
    )
    service = _service(session, store, observer)
    observation = _execute_observation(service, target, card, key="review-replay")

    first = service.review(
        CUSTOMER,
        observation.run_id,
        decision="approved",
        reason="人工核对照片，类别一致",
        confirmed=True,
    )
    replay = service.review(
        CUSTOMER,
        observation.run_id,
        decision="approved",
        reason="人工核对照片，类别一致",
        confirmed=True,
    )
    with pytest.raises(
        DomainRuleError, match="product_identity_observation_review_conflict"
    ):
        service.review(
            CUSTOMER,
            observation.run_id,
            decision="rejected",
            reason="改变决定",
            confirmed=True,
        )

    assert replay == first
    assert replay.created is False


def test_rejected_review_and_direct_block_cannot_enable_generation(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    needs_review = _service(
        session,
        store,
        RecordingObserver(
            payload=_identity_payload(category={"value": None, "certainty": "unclear"})
        ),
    )
    observation = _execute_observation(
        needs_review, target, card, key="review-rejection"
    )
    review = needs_review.review(
        CUSTOMER,
        observation.run_id,
        decision="rejected",
        reason="人工无法确认是同一商品",
        confirmed=True,
    )
    with pytest.raises(
        DomainRuleError, match="product_identity_observation_not_approved"
    ):
        needs_review.validate_generation_evidence(
            CUSTOMER,
            target.product_id,
            target.id,
            card.id,
            observation.run_id,
            review_id=review.review_id,
        )

    blocked = _service(
        session,
        store,
        RecordingObserver(
            payload=_identity_payload(
                core_accessories={
                    "value": [
                        {"kind": "clasp", "visible_count": 1},
                        {"kind": "pendant", "visible_count": 1},
                    ],
                    "certainty": "observed",
                }
            )
        ),
    )
    blocked_observation = _execute_observation(
        blocked, target, card, key="direct-block"
    )
    with pytest.raises(
        DomainRuleError, match="product_identity_observation_blocked"
    ):
        blocked.review(
            CUSTOMER,
            blocked_observation.run_id,
            decision="approved",
            reason="不能绕过明确结构冲突",
            confirmed=True,
        )


def test_pass_needs_no_review_and_approval_is_invalidated_by_withdrawal(
    session: Session, graph
) -> None:
    _, target, card, store = graph
    passing = _service(session, store, RecordingObserver())
    passed = _execute_observation(passing, target, card, key="pass-evidence")
    evidence = passing.validate_generation_evidence(
        CUSTOMER,
        target.product_id,
        target.id,
        card.id,
        passed.run_id,
    )
    assert evidence.review_id is None

    with pytest.raises(
        DomainRuleError, match="product_identity_observation_review_invalid"
    ):
        passing.review(
            CUSTOMER,
            passed.run_id,
            decision="approved",
            reason="通过记录不需要人工审核",
            confirmed=True,
        )

    session.add(
        ProductAssetPermissionEvent(
            customer_id=CUSTOMER,
            product_asset_id=target.id,
            transform_permission=False,
            occurred_at=NOW + timedelta(seconds=1),
            provenance="customer_permission_withdrawal",
        )
    )
    session.commit()
    with pytest.raises(DomainRuleError, match="transform_permission_denied"):
        passing.validate_generation_evidence(
            CUSTOMER,
            target.product_id,
            target.id,
            card.id,
            passed.run_id,
        )
