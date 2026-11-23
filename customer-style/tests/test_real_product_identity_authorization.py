"""Single-dispatch authorization and human confirmation for identity cards."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from opc_style.db import create_database_engine
from opc_style.domain import (
    ProductIdentityDraft,
    ProductIdentityImage,
    identity_card_payload,
    identity_card_sha256,
    normalize_identity_draft,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    Base,
    Customer,
    ProductAsset,
    ProductIdentityAnalysisRun,
    ProductIdentityCardVersion,
)
from opc_style.services.product_identity import ProductIdentityService
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER_A = UUID("00000000-0000-0000-0000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-0000-0000-0000000000b2")
ASSET_A = UUID("10000000-0000-0000-0000-0000000000a1")
MODEL = "recording-product-identity-v1"
MAXIMUM_USD = Decimal("0.125000")
IMAGE_BYTES = b"synthetic-product-image-a"
IMAGE_SHA256 = sha256(IMAGE_BYTES).hexdigest()


def identity_payload(**field_values: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "category": {"value": "bracelet", "certainty": "observed"},
        "primary_colors": {"value": ["green", "gold_tone"], "certainty": "observed"},
        "visible_surface_appearance": {
            "value": ["translucent", "glossy"],
            "certainty": "observed",
        },
        "component_shapes": {"value": ["round_bead"], "certainty": "observed"},
        "visible_component_count": {"value": 18, "certainty": "observed"},
        "component_arrangement": {"value": "single_strand", "certainty": "observed"},
        "core_accessories": {
            "value": [{"kind": "clasp", "visible_count": 1}],
            "certainty": "observed",
        },
        "overall_form": {"value": "closed_loop", "certainty": "observed"},
        "relative_proportions": {
            "value": "主体珠大小接近，扣头明显更小",
            "certainty": "observed",
        },
        "distinguishing_marks": {
            "value": "一颗主体珠有明显深色斑点",
            "certainty": "observed",
        },
        "visibility_limits": {"value": ["clasp_hidden"], "certainty": "observed"},
    }
    for field, value in field_values.items():
        assert field in payload
        payload[field] = value
    return payload


class RecordingAnalyzer:
    analyzer_version = "recording-product-identity-v1"
    analysis_mode = "real_product_identity_proposal"

    def __init__(
        self,
        session: Session | None = None,
        *,
        failure: str | None = None,
        proposal: ProductIdentityDraft | None = None,
    ) -> None:
        self.calls = 0
        self.images: list[ProductIdentityImage] = []
        self.ledger_at_dispatch: list[tuple[str, int]] = []
        self._session = session
        self._failure = failure
        self._proposal = proposal or normalize_identity_draft(identity_payload())

    def analyze(self, image: ProductIdentityImage) -> ProductIdentityDraft:
        self.calls += 1
        self.images.append(image)
        if self._session is not None:
            run = self._session.scalar(select(ProductIdentityAnalysisRun))
            assert run is not None
            self.ledger_at_dispatch.append((run.dispatch_state, run.outbound_attempts))
        if self._failure is not None:
            raise DomainRuleError(self._failure)
        return self._proposal


@pytest.fixture
def product_asset(session: Session, customer_factory) -> ProductAsset:
    customer_factory(CUSTOMER_A)
    asset = ProductAsset(
        id=ASSET_A,
        customer_id=CUSTOMER_A,
        product_id="product-a",
        photo_id="photo-a",
        identity="original_real_photo",
        confirmed=True,
        source_type="classification_handoff",
        quality_flags=[],
        occurred_at=datetime(2026, 9, 12, tzinfo=UTC),
        content_sha256=IMAGE_SHA256,
        mime_type="image/png",
        width=32,
        height=24,
        storage_path=None,
    )
    session.add(asset)
    session.commit()
    return asset


@pytest.fixture
def content_store(tmp_path, product_asset: ProductAsset):
    with LocalSyntheticAssetStore(tmp_path / "identity-content") as store:
        store.persist_original(CUSTOMER_A, product_asset.id, IMAGE_BYTES, IMAGE_SHA256)
        yield store


@pytest.fixture
def analyzer(session: Session) -> RecordingAnalyzer:
    return RecordingAnalyzer(session)


@pytest.fixture
def service(session: Session, content_store, analyzer: RecordingAnalyzer):
    return ProductIdentityService(
        session,
        content_store=content_store,
        analyzer=analyzer,
        model=MODEL,
        maximum_usd=MAXIMUM_USD,
    )


def _execute(service: ProductIdentityService, key: str = "identity-1"):
    preview = service.preview(CUSTOMER_A, ASSET_A)
    return service.execute(
        CUSTOMER_A,
        ASSET_A,
        idempotency_key=key,
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )


def test_preview_reads_store_verified_asset_without_provider_call(
    service: ProductIdentityService,
    analyzer: RecordingAnalyzer,
) -> None:
    preview = service.preview(CUSTOMER_A, ASSET_A)

    assert preview.asset_id == ASSET_A
    assert preview.product_id == "product-a"
    assert preview.source_content_sha256 == IMAGE_SHA256
    assert preview.model == MODEL
    assert preview.maximum_usd == MAXIMUM_USD
    assert preview.currency == "USD"
    assert preview.input_image_count == 1
    assert len(preview.manifest_sha256) == 64
    assert preview.publishable is False
    assert analyzer.calls == 0


@pytest.mark.parametrize(
    ("confirmed_maximum_usd", "confirmed_manifest_sha256"),
    ((Decimal(0), None), (None, "f" * 64)),
)
def test_wrong_cost_or_manifest_never_calls_provider(
    service: ProductIdentityService,
    analyzer: RecordingAnalyzer,
    confirmed_maximum_usd: Decimal | None,
    confirmed_manifest_sha256: str | None,
) -> None:
    preview = service.preview(CUSTOMER_A, ASSET_A)

    with pytest.raises(
        DomainRuleError, match="^product_identity_analysis_confirmation_required$"
    ):
        service.execute(
            CUSTOMER_A,
            ASSET_A,
            idempotency_key=f"identity-bad-{confirmed_maximum_usd}",
            confirmed_maximum_usd=(
                confirmed_maximum_usd
                if confirmed_maximum_usd is not None
                else preview.maximum_usd
            ),
            confirmed_manifest_sha256=(
                confirmed_manifest_sha256
                if confirmed_manifest_sha256 is not None
                else preview.manifest_sha256
            ),
        )

    assert analyzer.calls == 0


def test_execute_once_replays_and_confirm_creates_one_normalized_version(
    service: ProductIdentityService,
    analyzer: RecordingAnalyzer,
) -> None:
    preview = service.preview(CUSTOMER_A, ASSET_A)
    first = service.execute(
        CUSTOMER_A,
        ASSET_A,
        idempotency_key="identity-1",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )
    replay = service.execute(
        CUSTOMER_A,
        ASSET_A,
        idempotency_key="identity-1",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )
    submitted = identity_card_payload(first.proposal)
    submitted["relative_proportions"] = {
        "value": "  主体珠大小接近，扣头明显更小  ",
        "certainty": "observed",
    }
    card = service.confirm(
        CUSTOMER_A,
        ASSET_A,
        analysis_run_id=first.run_id,
        card=submitted,
        confirmed=True,
    )
    same = service.confirm(
        CUSTOMER_A,
        ASSET_A,
        analysis_run_id=first.run_id,
        card=identity_card_payload(first.proposal),
        confirmed=True,
    )

    assert analyzer.calls == 1
    assert analyzer.ledger_at_dispatch == [("dispatch_uncertain", 1)]
    assert analyzer.images == [
        ProductIdentityImage(
            asset_id=ASSET_A,
            product_id="product-a",
            content=IMAGE_BYTES,
            content_sha256=IMAGE_SHA256,
            mime_type="image/png",
        )
    ]
    assert first.created is True
    assert replay == replace(first, created=False)
    assert card == same
    assert card.created is True
    assert same.created is False
    assert card.version == 1
    assert card.card == first.proposal
    assert card.card_sha256 == identity_card_sha256(first.proposal)


def test_completed_replay_still_requires_exact_cost_and_manifest_confirmation(
    service: ProductIdentityService,
    analyzer: RecordingAnalyzer,
) -> None:
    preview = service.preview(CUSTOMER_A, ASSET_A)
    service.execute(
        CUSTOMER_A,
        ASSET_A,
        idempotency_key="confirmed-replay",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )

    for cost, manifest in (
        (Decimal(0), preview.manifest_sha256),
        (preview.maximum_usd, "f" * 64),
    ):
        with pytest.raises(
            DomainRuleError, match="^product_identity_analysis_confirmation_required$"
        ):
            service.execute(
                CUSTOMER_A,
                ASSET_A,
                idempotency_key="confirmed-replay",
                confirmed_maximum_usd=cost,
                confirmed_manifest_sha256=manifest,
            )

    assert analyzer.calls == 1


def test_completed_replay_is_bound_to_the_stored_model(
    session: Session,
    content_store,
    analyzer: RecordingAnalyzer,
) -> None:
    original = ProductIdentityService(
        session,
        content_store=content_store,
        analyzer=analyzer,
        model=MODEL,
        maximum_usd=MAXIMUM_USD,
    )
    preview = original.preview(CUSTOMER_A, ASSET_A)
    original.execute(
        CUSTOMER_A,
        ASSET_A,
        idempotency_key="model-bound-replay",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )
    restarted = ProductIdentityService(
        session,
        content_store=content_store,
        analyzer=analyzer,
        model="different-product-identity-model",
        maximum_usd=MAXIMUM_USD,
    )

    with pytest.raises(
        DomainRuleError, match="^product_identity_analysis_confirmation_required$"
    ):
        restarted.execute(
            CUSTOMER_A,
            ASSET_A,
            idempotency_key="model-bound-replay",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.manifest_sha256,
        )

    assert analyzer.calls == 1


def test_high_precision_cost_is_canonical_before_preview_persistence_and_replay(
    session: Session,
    content_store,
    analyzer: RecordingAnalyzer,
) -> None:
    service = ProductIdentityService(
        session,
        content_store=content_store,
        analyzer=analyzer,
        model=MODEL,
        maximum_usd=Decimal("0.1234567"),
    )
    preview = service.preview(CUSTOMER_A, ASSET_A)
    first = service.execute(
        CUSTOMER_A,
        ASSET_A,
        idempotency_key="high-precision-cost",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )
    replay = service.execute(
        CUSTOMER_A,
        ASSET_A,
        idempotency_key="high-precision-cost",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )

    run = session.get(ProductIdentityAnalysisRun, first.run_id)
    assert run is not None
    assert preview.maximum_usd == Decimal("0.123457")
    assert run.authorized_usd == Decimal("0.123457")
    assert replay == replace(first, created=False)
    assert analyzer.calls == 1


def test_provider_failure_is_terminal_and_never_auto_retries(
    session: Session,
    content_store,
) -> None:
    analyzer = RecordingAnalyzer(
        session, failure="product_identity_analysis_unavailable"
    )
    service = ProductIdentityService(
        session,
        content_store=content_store,
        analyzer=analyzer,
        model=MODEL,
        maximum_usd=MAXIMUM_USD,
    )
    preview = service.preview(CUSTOMER_A, ASSET_A)

    for _ in range(2):
        with pytest.raises(
            DomainRuleError, match="^product_identity_analysis_outcome_uncertain$"
        ):
            service.execute(
                CUSTOMER_A,
                ASSET_A,
                idempotency_key="failed-identity",
                confirmed_maximum_usd=preview.maximum_usd,
                confirmed_manifest_sha256=preview.manifest_sha256,
            )

    assert analyzer.calls == 1
    run = session.scalar(
        select(ProductIdentityAnalysisRun).where(
            ProductIdentityAnalysisRun.idempotency_key == "failed-identity"
        )
    )
    assert run is not None
    assert run.dispatch_state == "dispatch_uncertain"
    assert run.outbound_attempts == 1
    assert run.failure_code == "product_identity_analysis_unavailable"


def test_replay_rejects_invalid_stored_proposal_without_provider_call(
    session: Session,
    service: ProductIdentityService,
    analyzer: RecordingAnalyzer,
) -> None:
    outcome = _execute(service, "invalid-stored-proposal")
    run = session.get(ProductIdentityAnalysisRun, outcome.run_id)
    assert run is not None
    run.proposal = {"category": {"value": "bracelet", "certainty": "observed"}}
    session.commit()

    with pytest.raises(
        DomainRuleError, match="^product_identity_analysis_outcome_uncertain$"
    ):
        _execute(service, "invalid-stored-proposal")

    assert analyzer.calls == 1


def test_cross_customer_asset_is_invisible_and_never_calls_provider(
    service: ProductIdentityService,
    analyzer: RecordingAnalyzer,
    customer_factory,
) -> None:
    customer_factory(CUSTOMER_B)

    with pytest.raises(DomainRuleError, match="^product_identity_asset_unavailable$"):
        service.preview(CUSTOMER_B, ASSET_A)

    assert analyzer.calls == 0


def test_service_digest_checks_bytes_even_if_store_adapter_does_not(
    session: Session,
    product_asset: ProductAsset,
) -> None:
    class WrongBytesStore:
        def read_original(
            self, customer_id: UUID, asset_id: UUID, expected_sha256: str
        ) -> bytes:
            del customer_id, asset_id, expected_sha256
            return b"wrong-content"

    analyzer = RecordingAnalyzer(session)
    service = ProductIdentityService(
        session,
        content_store=WrongBytesStore(),
        analyzer=analyzer,
        model=MODEL,
        maximum_usd=MAXIMUM_USD,
    )

    with pytest.raises(DomainRuleError, match="^product_identity_asset_unavailable$"):
        service.preview(CUSTOMER_A, product_asset.id)

    assert analyzer.calls == 0


def test_source_mutation_during_provider_call_is_terminal(
    session: Session,
    product_asset: ProductAsset,
) -> None:
    class MutableStore:
        def __init__(self) -> None:
            self.changed = False

        def read_original(
            self, customer_id: UUID, asset_id: UUID, expected_sha256: str
        ) -> bytes:
            del customer_id, asset_id, expected_sha256
            return b"changed" if self.changed else IMAGE_BYTES

    store = MutableStore()

    class MutatingAnalyzer(RecordingAnalyzer):
        def analyze(self, image: ProductIdentityImage) -> ProductIdentityDraft:
            result = super().analyze(image)
            store.changed = True
            return result

    analyzer = MutatingAnalyzer(session)
    service = ProductIdentityService(
        session,
        content_store=store,
        analyzer=analyzer,
        model=MODEL,
        maximum_usd=MAXIMUM_USD,
    )
    preview = service.preview(CUSTOMER_A, product_asset.id)

    with pytest.raises(
        DomainRuleError, match="^product_identity_analysis_outcome_uncertain$"
    ):
        service.execute(
            CUSTOMER_A,
            product_asset.id,
            idempotency_key="mutated-source",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.manifest_sha256,
        )

    assert analyzer.calls == 1
    run = session.scalar(
        select(ProductIdentityAnalysisRun).where(
            ProductIdentityAnalysisRun.idempotency_key == "mutated-source"
        )
    )
    assert run is not None
    assert run.dispatch_state == "dispatch_uncertain"
    assert run.outbound_attempts == 1
    assert run.proposal is None
    assert run.failure_code == "product_identity_manifest_changed"


def test_post_success_source_mutation_blocks_replay_without_new_provider_call(
    service: ProductIdentityService,
    analyzer: RecordingAnalyzer,
    content_store,
) -> None:
    preview = service.preview(CUSTOMER_A, ASSET_A)
    service.execute(
        CUSTOMER_A,
        ASSET_A,
        idempotency_key="mutated-after-success",
        confirmed_maximum_usd=preview.maximum_usd,
        confirmed_manifest_sha256=preview.manifest_sha256,
    )
    content_store.resolve_original(CUSTOMER_A, ASSET_A).write_bytes(b"corrupt")

    with pytest.raises(DomainRuleError, match="^product_identity_manifest_changed$"):
        service.execute(
            CUSTOMER_A,
            ASSET_A,
            idempotency_key="mutated-after-success",
            confirmed_maximum_usd=preview.maximum_usd,
            confirmed_manifest_sha256=preview.manifest_sha256,
        )

    assert analyzer.calls == 1


def test_post_success_source_mutation_blocks_confirmation(
    service: ProductIdentityService,
    session: Session,
    content_store,
) -> None:
    outcome = _execute(service, "mutated-before-confirmation")
    content_store.resolve_original(CUSTOMER_A, ASSET_A).write_bytes(b"corrupt")

    with pytest.raises(DomainRuleError, match="^product_identity_manifest_changed$"):
        service.confirm(
            CUSTOMER_A,
            ASSET_A,
            analysis_run_id=outcome.run_id,
            card=identity_card_payload(outcome.proposal),
            confirmed=True,
        )

    assert (
        session.scalar(select(func.count()).select_from(ProductIdentityCardVersion))
        == 0
    )


def test_post_success_source_mutation_blocks_confirmed_version_listing(
    service: ProductIdentityService,
    content_store,
) -> None:
    outcome = _execute(service, "mutated-before-listing")
    service.confirm(
        CUSTOMER_A,
        ASSET_A,
        analysis_run_id=outcome.run_id,
        card=identity_card_payload(outcome.proposal),
        confirmed=True,
    )
    content_store.resolve_original(CUSTOMER_A, ASSET_A).write_bytes(b"corrupt")

    with pytest.raises(DomainRuleError, match="^product_identity_manifest_changed$"):
        service.versions(CUSTOMER_A, ASSET_A)


def test_confirmation_requires_exact_human_true_and_complete_card(
    service: ProductIdentityService,
    session: Session,
) -> None:
    outcome = _execute(service)

    with pytest.raises(
        DomainRuleError, match="^product_identity_card_confirmation_required$"
    ):
        service.confirm(
            CUSTOMER_A,
            ASSET_A,
            analysis_run_id=outcome.run_id,
            card=identity_card_payload(outcome.proposal),
            confirmed=False,
        )
    with pytest.raises(DomainRuleError, match="^product_identity_card_incomplete$"):
        service.confirm(
            CUSTOMER_A,
            ASSET_A,
            analysis_run_id=outcome.run_id,
            card={"category": {"value": "bracelet", "certainty": "observed"}},
            confirmed=True,
        )

    assert (
        session.scalar(select(func.count()).select_from(ProductIdentityCardVersion))
        == 0
    )


def test_confirmation_rejects_cross_asset_or_customer_analysis_lineage(
    service: ProductIdentityService,
    customer_factory,
) -> None:
    outcome = _execute(service)
    customer_factory(CUSTOMER_B)

    for customer_id, asset_id in (
        (CUSTOMER_B, ASSET_A),
        (CUSTOMER_A, uuid4()),
    ):
        with pytest.raises(
            DomainRuleError, match="^product_identity_card_scope_mismatch$"
        ):
            service.confirm(
                customer_id,
                asset_id,
                analysis_run_id=outcome.run_id,
                card=identity_card_payload(outcome.proposal),
                confirmed=True,
            )


def test_edited_confirmation_appends_gap_free_versions_and_lists_newest_first(
    service: ProductIdentityService,
) -> None:
    outcome = _execute(service)
    original = identity_card_payload(outcome.proposal)
    edited = deepcopy(original)
    edited["distinguishing_marks"] = {
        "value": "两颗主体珠有可见深色斑点",
        "certainty": "observed",
    }

    first = service.confirm(
        CUSTOMER_A,
        ASSET_A,
        analysis_run_id=outcome.run_id,
        card=original,
        confirmed=True,
    )
    second = service.confirm(
        CUSTOMER_A,
        ASSET_A,
        analysis_run_id=outcome.run_id,
        card=edited,
        confirmed=True,
    )

    assert [first.version, second.version] == [1, 2]
    assert [row.version for row in service.versions(CUSTOMER_A, ASSET_A)] == [2, 1]


def test_customer_coordinator_serializes_concurrent_gap_free_versions(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'identity-concurrent.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with LocalSyntheticAssetStore(tmp_path / "identity-concurrent-store") as store:
        with factory() as seed:
            seed.add(Customer(id=CUSTOMER_A, display_name="Customer A"))
            asset = ProductAsset(
                id=ASSET_A,
                customer_id=CUSTOMER_A,
                product_id="product-a",
                photo_id="photo-a",
                identity="original_real_photo",
                confirmed=True,
                source_type="classification_handoff",
                quality_flags=[],
                occurred_at=datetime(2026, 9, 12, tzinfo=UTC),
                content_sha256=IMAGE_SHA256,
                mime_type="image/png",
                width=32,
                height=24,
                storage_path=None,
            )
            seed.add(asset)
            seed.commit()
            store.persist_original(CUSTOMER_A, ASSET_A, IMAGE_BYTES, IMAGE_SHA256)
            analyzer = RecordingAnalyzer(seed)
            seeded_service = ProductIdentityService(
                seed,
                content_store=store,
                analyzer=analyzer,
                model=MODEL,
                maximum_usd=MAXIMUM_USD,
            )
            analysis = _execute(seeded_service, "concurrent-confirmation")

        barrier = Barrier(2)

        def confirm_version(mark: str) -> int:
            payload = identity_payload(
                distinguishing_marks={"value": mark, "certainty": "observed"}
            )
            with factory() as worker_session:
                worker_service = ProductIdentityService(
                    worker_session,
                    content_store=store,
                    analyzer=RecordingAnalyzer(),
                    model=MODEL,
                    maximum_usd=MAXIMUM_USD,
                )
                barrier.wait(timeout=5)
                return worker_service.confirm(
                    CUSTOMER_A,
                    ASSET_A,
                    analysis_run_id=analysis.run_id,
                    card=payload,
                    confirmed=True,
                ).version

        with ThreadPoolExecutor(max_workers=2) as executor:
            assigned = sorted(
                executor.map(confirm_version, ("可见深色斑点", "可见浅色条纹"))
            )

        with factory() as check:
            persisted = list(
                check.scalars(
                    select(ProductIdentityCardVersion).order_by(
                        ProductIdentityCardVersion.version
                    )
                )
            )

    Base.metadata.drop_all(engine)
    engine.dispose()
    assert assigned == [1, 2]
    assert [row.version for row in persisted] == [1, 2]
