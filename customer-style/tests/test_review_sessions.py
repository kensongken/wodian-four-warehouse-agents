"""Persistence contracts for customer candidate-image review sessions."""

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, local
from uuid import UUID, uuid4

import pytest
import test_image_review_api as review_api_fixtures
from alembic.config import Config
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from opc_style.db import create_database_engine
from opc_style.domain.authenticity import DeterministicLocalAuthenticityChecker
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import (
    DeterministicLocalImageGenerator,
    GeneratedCandidate,
    GenerationRequest,
    ImageGenerationError,
)
from opc_style.models import (
    ApprovedImageAssociation,
    Base,
    Customer,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    ImageReviewRound,
    ImageReviewSession,
    SceneStyle,
)
from opc_style.repositories import ReviewSessionRepository
from opc_style.services.authenticity import AuthenticityService
from opc_style.services.image_jobs import (
    ImageGenerationInProgressError,
    ImageJobService,
    ImageSourceSelection,
)
from opc_style.services.image_reviews import (
    ImageReviewConflictError,
    ImageReviewNotFoundError,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.services.review_sessions import ReviewSessionService

CUSTOMER_A_ID = UUID("00000000-0000-0000-0000-0000000000a1")
CUSTOMER_B_ID = UUID("00000000-0000-0000-0000-0000000000b2")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Reuse real, file-backed image pipeline fixtures without mocking persistence.
api_session_factory = review_api_fixtures.api_session_factory
asset_factory = review_api_fixtures.asset_factory
content_store = review_api_fixtures.content_store
style_factory = review_api_fixtures.style_factory


class ReviewGenerator(DeterministicLocalImageGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.before_generate: Callable[[], None] | None = None
        self.fail = False

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        self.calls += 1
        if self.before_generate is not None:
            self.before_generate()
        if self.fail:
            raise ImageGenerationError("synthetic_generator_failure")
        return super().generate(request)


@pytest.fixture
def workflow(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    style_factory: Callable,
    asset_factory: Callable,
) -> Generator[
    tuple[Session, ReviewSessionService, ReviewGenerator, UUID, UUID], None, None
]:
    scene, version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory(customer_id=CUSTOMER_A_ID, product_id="product-a")
    with api_session_factory() as session:
        generated = ImageJobService(
            session, content_store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=(ImageSourceSelection("product-a", asset.id),),
            scene_style_id=scene.id,
            style_card_version_id=version.id,
            output_ratio="1:1",
            idempotency_key="initial-review-job",
        )
        AuthenticityService(
            session, content_store, DeterministicLocalAuthenticityChecker()
        ).run(CUSTOMER_A_ID, generated.job.id)
        generator = ReviewGenerator()
        adapter = DeterministicLocalImageGenerator()
        adapter.generate = generator.generate  # type: ignore[method-assign]
        service = ReviewSessionService(session, content_store, adapter)
        opened = service.open(CUSTOMER_A_ID, uuid4(), generated.job.id, "product-a")
        yield session, service, generator, opened.session.id, generated.candidates[0].id


def test_open_replays_handoff_and_rejects_wrong_product(workflow: tuple) -> None:
    session, service, _, session_id, _ = workflow
    row = session.get(ImageReviewSession, session_id)
    replay = service.open(
        CUSTOMER_A_ID, row.initial_handoff_id, row.current_job_id, "product-a"
    )
    assert replay.session.id == session_id
    assert replay.created is False
    with pytest.raises(ImageReviewNotFoundError):
        service.open(CUSTOMER_A_ID, uuid4(), row.current_job_id, "foreign-product")


def test_different_handoff_for_same_job_returns_the_original_session(
    workflow: tuple,
) -> None:
    session, service, _, session_id, _ = workflow
    row = session.get(ImageReviewSession, session_id)
    replay = service.open(CUSTOMER_A_ID, uuid4(), row.current_job_id, "product-a")
    assert replay.session.id == session_id
    assert replay.created is False
    assert session.scalar(select(func.count()).select_from(ImageReviewSession)) == 1
    assert session.scalar(text("SELECT count(*) FROM image_review_session_jobs")) == 1


def test_regenerated_job_is_bound_before_it_can_open_another_session(
    workflow: tuple,
) -> None:
    session, service, _, session_id, candidate_id = workflow
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="bound-regeneration",
    )
    replay = service.open(CUSTOMER_A_ID, uuid4(), result.next_job.id, "product-a")
    assert replay.session.id == session_id
    assert replay.created is False
    assert session.scalar(select(func.count()).select_from(ImageReviewSession)) == 1
    assert session.scalar(text("SELECT count(*) FROM image_review_session_jobs")) == 2


def test_concurrent_first_opens_leave_one_binding_and_one_session(
    workflow: tuple,
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    session, _, _, session_id, _ = workflow
    initial = session.get(
        ImageOutputJob, session.get(ImageReviewSession, session_id).current_job_id
    )
    generated = ImageJobService(
        session, content_store, DeterministicLocalImageGenerator()
    ).create(
        CUSTOMER_A_ID,
        output_type=initial.output_type,
        sources=tuple(
            ImageSourceSelection(source["product_id"], UUID(source["asset_id"]))
            for source in initial.request_metadata["sources"]
        ),
        scene_style_id=initial.scene_style_id,
        style_card_version_id=initial.style_card_version_id,
        output_ratio=initial.output_ratio,
        idempotency_key="concurrent-open-job",
    )
    AuthenticityService(
        session, content_store, DeterministicLocalAuthenticityChecker()
    ).run(CUSTOMER_A_ID, generated.job.id)
    barrier = Barrier(2)
    reached = local()
    engine = session.get_bind()
    job_id = generated.job.id

    def synchronize_first_lookup(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "FROM image_review_session_jobs" in statement and not getattr(
            reached, "waited", False
        ):
            reached.waited = True
            barrier.wait(timeout=10)

    def open_session(_: int) -> UUID:
        with api_session_factory() as worker:
            service = ReviewSessionService(
                worker, content_store, DeterministicLocalImageGenerator()
            )
            return service.open(CUSTOMER_A_ID, uuid4(), job_id, "product-a").session.id

    event.listen(engine, "after_cursor_execute", synchronize_first_lookup)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            session_ids = list(workers.map(open_session, range(2)))
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_first_lookup)
    assert session_ids[0] == session_ids[1]
    assert session.scalar(select(func.count()).select_from(ImageReviewSession)) == 2
    assert session.scalar(text("SELECT count(*) FROM image_review_session_jobs")) == 2


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_review_session_job_binding_rejects_orm_mutation(
    workflow: tuple,
    operation: str,
) -> None:
    from opc_style.models import ImageReviewSessionJob

    session, _, _, _, _ = workflow
    binding = session.scalar(select(ImageReviewSessionJob))
    if operation == "update":
        binding.session_id = uuid4()
    else:
        session.delete(binding)
    with pytest.raises(DomainRuleError, match="image_review_session_job_immutable"):
        session.commit()


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_review_session_job_binding_rejects_native_mutation(
    workflow: tuple,
    operation: str,
) -> None:
    session, _, _, _, _ = workflow
    query = (
        "UPDATE image_review_session_jobs SET session_id = session_id"
        if operation == "UPDATE"
        else "DELETE FROM image_review_session_jobs"
    )
    with pytest.raises(IntegrityError, match="image_review_session_job_immutable"):
        session.execute(text(query))


def test_review_session_job_binding_rejects_foreign_customer_references(
    session: Session,
) -> None:
    from opc_style.models import ImageReviewSessionJob

    session.add_all(
        [
            Customer(id=CUSTOMER_A_ID, display_name="A"),
            Customer(id=CUSTOMER_B_ID, display_name="B"),
        ]
    )
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    row = _review_session(CUSTOMER_A_ID, job.id)
    session.add(row)
    session.flush()
    session.add(
        ImageReviewSessionJob(
            customer_id=CUSTOMER_B_ID, job_id=job.id, session_id=row.id
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("ambiguous_owner", [False, True])
def test_binding_migration_backfills_owned_history_and_rejects_ambiguous_jobs(
    tmp_path: Path,
    ambiguous_owner: bool,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'binding-upgrade.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0013_review_sessions")
    engine = create_database_engine(database_url)
    with Session(engine) as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="A"))
        original, candidate, review = _reviewed_job(session, CUSTOMER_A_ID)
        current = _job(CUSTOMER_A_ID)
        session.add(current)
        session.flush()
        row = _insert_legacy_review_session(session, current.id)
        session.add(_review_round(CUSTOMER_A_ID, row, original, candidate, review))
        if ambiguous_owner:
            _insert_legacy_review_session(session, original.id)
        session.commit()
    if ambiguous_owner:
        with pytest.raises(
            RuntimeError, match="ambiguous review session job ownership"
        ):
            command.upgrade(config, "head")
    else:
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM image_review_session_jobs")
                )
                == 2
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(DISTINCT session_id) FROM image_review_session_jobs"
                    )
                )
                == 1
            )
    engine.dispose()


def test_initial_job_upgrade_preserves_rounds_bindings_and_identity(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'initial-job-upgrade.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0013_review_sessions")
    engine = create_database_engine(database_url)
    with Session(engine) as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="A"))
        original, candidate, review = _reviewed_job(session, CUSTOMER_A_ID)
        current = _job(CUSTOMER_A_ID)
        session.add(current)
        session.flush()
        row = _insert_legacy_review_session(session, current.id)
        session.add(_review_round(CUSTOMER_A_ID, row, original, candidate, review))
        root_id, current_id, row_id = original.id, current.id, row.id
        session.commit()
    command.upgrade(config, "0014_review_session_jobs")
    with engine.connect() as connection:
        old_bindings = connection.execute(
            text("SELECT * FROM image_review_session_jobs ORDER BY id")
        ).all()
        old_rounds = connection.execute(
            text("SELECT * FROM image_review_rounds ORDER BY id")
        ).all()
    command.upgrade(config, "head")
    with Session(engine) as session:
        migrated = session.get(ImageReviewSession, row_id)
        assert migrated.initial_job_id == root_id
        assert migrated.current_job_id == current_id
        assert (
            session.execute(
                text("SELECT * FROM image_review_session_jobs ORDER BY id")
            ).all()
            == old_bindings
        )
        assert (
            session.execute(text("SELECT * FROM image_review_rounds ORDER BY id")).all()
            == old_rounds
        )
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert session.scalar(text("PRAGMA foreign_keys")) == 1
        with pytest.raises(
            IntegrityError, match="image_review_session_identity_immutable"
        ):
            session.execute(
                text(
                    "UPDATE image_review_sessions SET initial_job_id=:job WHERE id=:id"
                ),
                {"job": str(current_id), "id": str(row_id)},
            )
        session.rollback()
    engine.dispose()


def test_review_session_job_binding_blocks_downgrade(
    workflow: tuple,
) -> None:
    session, _, _, _, _ = workflow
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", str(session.get_bind().url))
    with pytest.raises(RuntimeError, match="cannot discard customer image review"):
        command.downgrade(config, "0013_review_sessions")


def test_approval_creates_immutable_association_and_replays(workflow: tuple) -> None:
    session, service, generator, session_id, candidate_id = workflow
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="approved",
        reason=None,
        idempotency_key="approve",
    )
    replay = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="approved",
        reason=None,
        idempotency_key="approve",
    )
    assert result.session.status == "approved"
    assert result.next_job is None
    assert replay.created is False
    assert (
        session.scalar(select(func.count()).select_from(ApprovedImageAssociation)) == 1
    )
    assert session.scalar(select(func.count()).select_from(ImageReviewRound)) == 1
    assert generator.calls == 0


def test_customer_key_conflict_across_sessions_precedes_immutable_review_side_effects(
    workflow: tuple,
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, service, _, first_session_id, first_candidate_id = workflow
    first_session = session.get(ImageReviewSession, first_session_id)
    first_job = session.get(ImageOutputJob, first_session.current_job_id)
    with api_session_factory() as other:
        generated = ImageJobService(
            other, content_store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type=first_job.output_type,
            sources=tuple(
                ImageSourceSelection(source["product_id"], UUID(source["asset_id"]))
                for source in first_job.request_metadata["sources"]
            ),
            scene_style_id=first_job.scene_style_id,
            style_card_version_id=first_job.style_card_version_id,
            output_ratio=first_job.output_ratio,
            idempotency_key="second-session-initial-job",
        )
        AuthenticityService(
            other, content_store, DeterministicLocalAuthenticityChecker()
        ).run(CUSTOMER_A_ID, generated.job.id)
        other_service = ReviewSessionService(
            other, content_store, DeterministicLocalImageGenerator()
        )
        opened = other_service.open(
            CUSTOMER_A_ID, uuid4(), generated.job.id, "product-a"
        )
        second_session_id = opened.session.id
        second_candidate_id = generated.candidates[0].id
        errors: dict[str, Exception] = {}
        interleaved = False
        commit = session.commit

        def commit_then_interleave_before_round() -> None:
            nonlocal interleaved
            review_count = session.scalar(select(func.count()).select_from(ImageReview))
            commit()
            if review_count and not interleaved:
                interleaved = True
                # The first immutable review is durable, but its round does not
                # exist yet. Run a real second workflow in an independent Session.
                assert (
                    other.scalar(select(func.count()).select_from(ImageReviewRound))
                    == 0
                )
                try:
                    other_service.decide(
                        CUSTOMER_A_ID,
                        second_session_id,
                        second_candidate_id,
                        decision="approved",
                        reason=None,
                        idempotency_key="one-customer-key",
                    )
                except (ImageReviewConflictError, IntegrityError) as error:
                    errors["second"] = error

        monkeypatch.setattr(session, "commit", commit_then_interleave_before_round)
        try:
            service.decide(
                CUSTOMER_A_ID,
                first_session_id,
                first_candidate_id,
                decision="approved",
                reason=None,
                idempotency_key="one-customer-key",
            )
        except (ImageReviewConflictError, IntegrityError) as error:
            errors["first"] = error
        assert interleaved
        assert session.scalar(select(func.count()).select_from(ImageReview)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ApprovedImageAssociation))
            == 1
        )
        assert session.scalar(select(func.count()).select_from(ImageReviewRound)) == 1
        assert "first" not in errors
        assert isinstance(errors.get("second"), ImageReviewConflictError)
        other.expire_all()
        assert (
            other.get(ImageOutputJob, generated.job.id).status
            == "awaiting_customer_review"
        )
        assert (
            other.get(ImageReviewSession, second_session_id).claim_owner_token is None
        )
        replay = service.decide(
            CUSTOMER_A_ID,
            first_session_id,
            first_candidate_id,
            decision="approved",
            reason=None,
            idempotency_key="one-customer-key",
        )
        assert replay.created is False
        assert replay.session.status == "approved"


@pytest.mark.parametrize(
    "decision", ["not_my_style", "product_detail_inaccurate", "rejected"]
)
def test_negative_decision_regenerates_sealed_inputs_and_replays(
    workflow: tuple,
    decision: str,
) -> None:
    session, service, generator, session_id, candidate_id = workflow
    row = session.get(ImageReviewSession, session_id)
    original = session.get(ImageOutputJob, row.current_job_id)
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision=decision,
        reason="less ornate",
        idempotency_key="negative",
    )
    replay = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision=decision,
        reason="less ornate",
        idempotency_key="negative",
    )
    assert result.next_job.id != original.id
    assert result.next_job.status == "awaiting_customer_review"
    assert result.session.current_job_id == result.next_job.id
    assert result.session.regeneration_count == 1
    assert result.session.claim_owner_token is None
    for key in (
        "sources",
        "source_asset_ids",
        "main_style_card_version_id",
        "scene_style_card_id",
    ):
        assert result.next_job.request_metadata[key] == original.request_metadata[key]
    assert result.next_job.style_card_version_id == original.style_card_version_id
    assert result.next_job.scene_style_id == original.scene_style_id
    assert result.next_job.output_type == original.output_type
    assert result.next_job.output_ratio == original.output_ratio
    assert replay.next_job.id == result.next_job.id
    assert replay.created is False
    assert generator.calls == 1


def test_fourth_negative_decision_requires_manual_attention_without_generation(
    workflow: tuple,
) -> None:
    session, service, generator, session_id, candidate_id = workflow
    first_candidate_id = candidate_id
    first_next_id = None
    for index in range(3):
        result = service.decide(
            CUSTOMER_A_ID,
            session_id,
            candidate_id,
            decision="not_my_style",
            reason=None,
            idempotency_key=f"negative-{index}",
        )
        first_next_id = first_next_id or result.next_job.id
        candidate_id = result.candidates[0].id
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="negative-4",
    )
    assert result.session.status == "manual_attention_required"
    assert result.session.regeneration_count == 3
    assert result.next_job is None
    assert generator.calls == 3
    assert session.scalar(select(func.count()).select_from(ImageReviewRound)) == 4
    replay = service.decide(
        CUSTOMER_A_ID,
        session_id,
        first_candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="negative-0",
    )
    assert replay.next_job.id == first_next_id
    assert generator.calls == 3


def test_wrong_customer_stale_candidate_and_conflicting_key_do_not_generate(
    workflow: tuple,
) -> None:
    _, service, generator, session_id, candidate_id = workflow
    with pytest.raises(ImageReviewNotFoundError):
        service.decide(
            CUSTOMER_B_ID,
            session_id,
            candidate_id,
            decision="not_my_style",
            reason=None,
            idempotency_key="foreign",
        )
    service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="negative",
    )
    with pytest.raises(ImageReviewConflictError):
        service.decide(
            CUSTOMER_A_ID,
            session_id,
            candidate_id,
            decision="approved",
            reason=None,
            idempotency_key="negative",
        )
    with pytest.raises(ImageReviewConflictError):
        service.decide(
            CUSTOMER_A_ID,
            session_id,
            candidate_id,
            decision="not_my_style",
            reason=None,
            idempotency_key="stale",
        )
    assert generator.calls == 1


def test_generation_lease_is_committed_before_generator_side_effect(
    workflow: tuple,
    api_session_factory: sessionmaker[Session],
) -> None:
    _, service, generator, session_id, candidate_id = workflow

    def verify_committed_lease() -> None:
        with api_session_factory() as observer:
            row = observer.get(ImageReviewSession, session_id)
            assert row.status == "regenerating"
            assert row.claim_owner_token is not None
            assert row.claim_expires_at is not None
            assert (
                observer.scalar(select(func.count()).select_from(ImageReviewRound)) == 1
            )

    generator.before_generate = verify_committed_lease
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="lease",
    )
    assert result.next_job.status == "awaiting_customer_review"


def test_failed_generation_releases_lease_and_replays_failure_without_new_job(
    workflow: tuple,
) -> None:
    _, service, generator, session_id, candidate_id = workflow
    generator.fail = True
    first = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="failed",
    )
    second = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="failed",
    )
    assert first.next_job.status == "generation_failed"
    assert second.next_job.id == first.next_job.id
    assert second.created is False
    assert second.session.status == "manual_attention_required"
    assert second.session.claim_owner_token is None
    assert generator.calls == 1


@pytest.mark.parametrize("second_key", ["concurrent", "other-concurrent"])
def test_live_lease_blocks_competing_decisions_without_duplicate_generation(
    workflow: tuple,
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    second_key: str,
) -> None:
    session, service, generator, session_id, candidate_id = workflow

    def compete() -> None:
        with api_session_factory() as other:
            competing = ReviewSessionService(
                other, content_store, DeterministicLocalImageGenerator()
            )
            with pytest.raises(ImageGenerationInProgressError):
                competing.decide(
                    CUSTOMER_A_ID,
                    session_id,
                    candidate_id,
                    decision="not_my_style",
                    reason=None,
                    idempotency_key=second_key,
                )

    generator.before_generate = compete
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="concurrent",
    )
    assert result.session.regeneration_count == 1
    assert session.scalar(select(func.count()).select_from(ImageReviewRound)) == 1
    assert session.scalar(select(func.count()).select_from(ImageOutputJob)) == 2
    assert generator.calls == 1


@pytest.mark.parametrize("decision", ["approved", "not_my_style"])
def test_retry_recovers_review_committed_before_round_write_failure(
    workflow: tuple,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    session, service, generator, session_id, candidate_id = workflow
    commit = session.commit

    def fail_round_commit_once() -> None:
        if any(isinstance(item, ImageReviewRound) for item in session.new):
            monkeypatch.setattr(session, "commit", commit)
            raise RuntimeError("simulated round commit failure")
        commit()

    monkeypatch.setattr(session, "commit", fail_round_commit_once)
    with pytest.raises(RuntimeError, match="simulated round commit failure"):
        service.decide(
            CUSTOMER_A_ID,
            session_id,
            candidate_id,
            decision=decision,
            reason=None,
            idempotency_key="recover-round",
        )
    assert session.get(ImageReviewSession, session_id).claim_owner_token is None
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision=decision,
        reason=None,
        idempotency_key="recover-round",
    )
    assert result.session.regeneration_count == (0 if decision == "approved" else 1)
    assert session.scalar(select(func.count()).select_from(ImageReview)) == 1
    assert session.scalar(select(func.count()).select_from(ImageReviewRound)) == 1
    assert session.scalar(
        select(func.count()).select_from(ApprovedImageAssociation)
    ) == (1 if decision == "approved" else 0)
    assert generator.calls == (0 if decision == "approved" else 1)


@pytest.mark.parametrize("deactivate_after_generation", [False, True])
def test_retry_recovers_generated_job_before_session_completion_failure(
    workflow: tuple,
    monkeypatch: pytest.MonkeyPatch,
    deactivate_after_generation: bool,
) -> None:
    session, service, generator, session_id, candidate_id = workflow
    complete = ReviewSessionRepository.complete_generation_claim

    def fail_completion_once(*args: object, **kwargs: object) -> None:
        monkeypatch.setattr(
            ReviewSessionRepository, "complete_generation_claim", complete
        )
        raise RuntimeError("simulated completion failure")

    monkeypatch.setattr(
        ReviewSessionRepository, "complete_generation_claim", fail_completion_once
    )
    with pytest.raises(RuntimeError, match="simulated completion failure"):
        service.decide(
            CUSTOMER_A_ID,
            session_id,
            candidate_id,
            decision="not_my_style",
            reason=None,
            idempotency_key="recover-job",
        )
    if deactivate_after_generation:
        scene = session.scalar(select(SceneStyle))
        scene.active = False
        session.commit()
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="recover-job",
    )
    assert result.next_job.status == "awaiting_customer_review"
    assert result.session.regeneration_count == 1
    assert session.scalar(select(func.count()).select_from(ImageOutputJob)) == 2
    assert generator.calls == 1


def test_stale_worker_cannot_release_replacement_owners_lease(
    workflow: tuple,
    api_session_factory: sessionmaker[Session],
) -> None:
    session, service, generator, session_id, candidate_id = workflow
    replacement_tokens: list[str] = []

    def take_expired_lease() -> None:
        with api_session_factory() as other:
            row = other.get(ImageReviewSession, session_id)
            claimed = ReviewSessionRepository(other).claim_for_generation(
                CUSTOMER_A_ID,
                session_id,
                expected_revision=row.revision,
                now=datetime.now(UTC) + timedelta(minutes=6),
            )
            replacement_tokens.append(claimed.claim_owner_token)
            other.commit()

    generator.before_generate = take_expired_lease
    with pytest.raises(ImageGenerationInProgressError):
        service.decide(
            CUSTOMER_A_ID,
            session_id,
            candidate_id,
            decision="not_my_style",
            reason=None,
            idempotency_key="stale-owner",
        )
    session.expire_all()
    row = session.get(ImageReviewSession, session_id)
    assert row.claim_owner_token == replacement_tokens[0]
    assert row.status == "regenerating"
    assert row.regeneration_count == 0


def test_lease_commit_error_releases_owned_claim_before_retry(
    workflow: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, service, generator, session_id, candidate_id = workflow
    commit = session.commit

    def commit_then_raise_once() -> None:
        commit()
        monkeypatch.setattr(session, "commit", commit)
        raise RuntimeError("simulated lost claim commit acknowledgement")

    monkeypatch.setattr(session, "commit", commit_then_raise_once)
    with pytest.raises(RuntimeError, match="lost claim commit acknowledgement"):
        service.decide(
            CUSTOMER_A_ID,
            session_id,
            candidate_id,
            decision="not_my_style",
            reason=None,
            idempotency_key="claim-commit",
        )
    session.expire_all()
    assert session.get(ImageReviewSession, session_id).claim_owner_token is None
    assert generator.calls == 0
    result = service.decide(
        CUSTOMER_A_ID,
        session_id,
        candidate_id,
        decision="not_my_style",
        reason=None,
        idempotency_key="claim-commit",
    )
    assert result.session.regeneration_count == 1
    assert generator.calls == 1


def _job(customer_id: UUID) -> ImageOutputJob:
    return ImageOutputJob(
        customer_id=customer_id,
        output_type="scene",
        output_ratio="1:1",
        adapter_version="deterministic-local-image-v1",
        idempotency_key=f"review-session-job-{uuid4()}",
    )


def _reviewed_job(
    session: Session, customer_id: UUID
) -> tuple[ImageOutputJob, ImageCandidate, ImageReview]:
    job = _job(customer_id)
    session.add(job)
    session.flush()
    candidate = ImageCandidate(
        customer_id=customer_id,
        output_job_id=job.id,
        sequence_number=1,
        identity="ai_style_generated",
        artifact_path="synthetic/review-candidate.jpg",
        content_sha256="a" * 64,
        mime_type="image/jpeg",
        width=1024,
        height=1024,
        adapter_version="deterministic-local-image-v1",
    )
    session.add(candidate)
    session.flush()
    review = ImageReview(
        customer_id=customer_id,
        image_candidate_id=candidate.id,
        decision="rejected",
        reason="synthetic-test-review",
        idempotency_key=f"review-{uuid4()}",
    )
    session.add(review)
    session.flush()
    return job, candidate, review


def _review_session(customer_id: UUID, job_id: UUID) -> ImageReviewSession:
    return ImageReviewSession(
        customer_id=customer_id,
        product_id="product-a",
        initial_handoff_id=uuid4(),
        current_job_id=job_id,
    )


def _insert_legacy_review_session(session: Session, job_id: UUID) -> ImageReviewSession:
    """Use the historical 0013/0014 shape instead of today's ORM columns."""
    row = _review_session(CUSTOMER_A_ID, job_id)
    row.id = uuid4()
    session.execute(
        text(
            "INSERT INTO image_review_sessions "
            "(id, customer_id, product_id, initial_handoff_id, current_job_id, created_at, updated_at) "
            "VALUES (:id, :customer, 'product-a', :handoff, :job, :now, :now)"
        ),
        {
            "id": str(row.id),
            "customer": str(CUSTOMER_A_ID),
            "handoff": str(row.initial_handoff_id),
            "job": str(job_id),
            "now": datetime.now(UTC),
        },
    )
    return row


def _review_round(
    customer_id: UUID,
    review_session: ImageReviewSession,
    job: ImageOutputJob,
    candidate: ImageCandidate,
    review: ImageReview,
    *,
    round_number: int = 1,
    idempotency_key: str | None = None,
) -> ImageReviewRound:
    return ImageReviewRound(
        customer_id=customer_id,
        session_id=review_session.id,
        round_number=round_number,
        output_job_id=job.id,
        image_review_id=review.id,
        image_candidate_id=candidate.id,
        idempotency_key=idempotency_key or f"round-{uuid4()}",
    )


def test_review_session_rejects_more_than_three_regenerations(session: Session) -> None:
    """A fourth regeneration must require human attention instead of persisting."""
    session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()

    review_session = ImageReviewSession(
        customer_id=CUSTOMER_A_ID,
        product_id="product-a",
        initial_handoff_id=uuid4(),
        current_job_id=job.id,
        regeneration_count=4,
        status="manual_attention_required",
    )
    session.add(review_session)

    with pytest.raises(IntegrityError):
        session.commit()


def test_review_round_rejects_a_duplicate_customer_idempotency_key(
    session: Session,
) -> None:
    """A retry cannot append a second round for the same customer request key."""
    session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
    job, candidate, review = _reviewed_job(session, CUSTOMER_A_ID)
    review_session = _review_session(CUSTOMER_A_ID, job.id)
    session.add(review_session)
    session.flush()

    session.add_all(
        [
            _review_round(
                CUSTOMER_A_ID,
                review_session,
                job,
                candidate,
                review,
                idempotency_key="retry-safe-round",
            ),
            _review_round(
                CUSTOMER_A_ID,
                review_session,
                job,
                candidate,
                review,
                round_number=2,
                idempotency_key="retry-safe-round",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_review_session_lookup_is_scoped_to_the_requesting_customer(
    session: Session,
) -> None:
    """A session ID alone must not disclose another customer's review workflow."""
    session.add_all(
        [
            Customer(id=CUSTOMER_A_ID, display_name="Customer A"),
            Customer(id=CUSTOMER_B_ID, display_name="Customer B"),
        ]
    )
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    review_session = _review_session(CUSTOMER_A_ID, job.id)
    session.add(review_session)
    session.flush()

    repository = ReviewSessionRepository(session)

    assert repository.get_for_update(CUSTOMER_A_ID, review_session.id) is review_session
    assert repository.get_for_update(CUSTOMER_B_ID, review_session.id) is None


def test_review_session_rejects_a_different_customers_current_job(
    session: Session,
) -> None:
    """A customer workflow cannot point its mutable cursor at another customer's job."""
    session.add_all(
        [
            Customer(id=CUSTOMER_A_ID, display_name="Customer A"),
            Customer(id=CUSTOMER_B_ID, display_name="Customer B"),
        ]
    )
    foreign_job = _job(CUSTOMER_B_ID)
    session.add(foreign_job)
    session.flush()
    session.add(_review_session(CUSTOMER_A_ID, foreign_job.id))

    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("mismatched_reference", ["session", "job", "review"])
def test_review_round_rejects_cross_customer_references(
    session: Session, mismatched_reference: str
) -> None:
    """Every round reference must belong to the same customer as the round."""
    session.add_all(
        [
            Customer(id=CUSTOMER_A_ID, display_name="Customer A"),
            Customer(id=CUSTOMER_B_ID, display_name="Customer B"),
        ]
    )
    job_a, candidate_a, review_a = _reviewed_job(session, CUSTOMER_A_ID)
    job_b, candidate_b, review_b = _reviewed_job(session, CUSTOMER_B_ID)
    review_session_a = _review_session(CUSTOMER_A_ID, job_a.id)
    review_session_b = _review_session(CUSTOMER_B_ID, job_b.id)
    session.add_all([review_session_a, review_session_b])
    session.flush()

    round_row = _review_round(
        CUSTOMER_A_ID, review_session_a, job_a, candidate_a, review_a
    )
    if mismatched_reference == "session":
        round_row.session_id = review_session_b.id
    elif mismatched_reference == "job":
        round_row.output_job_id = job_b.id
    else:
        round_row.image_review_id = review_b.id
        round_row.image_candidate_id = candidate_b.id
    session.add(round_row)

    with pytest.raises(IntegrityError):
        session.commit()


def test_review_round_rejects_missing_review_evidence(session: Session) -> None:
    """A round needs a durable customer review record, not only a free-form job ID."""
    session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
    job, candidate, review = _reviewed_job(session, CUSTOMER_A_ID)
    review_session = _review_session(CUSTOMER_A_ID, job.id)
    session.add(review_session)
    session.flush()
    round_row = _review_round(CUSTOMER_A_ID, review_session, job, candidate, review)
    round_row.image_review_id = uuid4()
    session.add(round_row)

    with pytest.raises(IntegrityError):
        session.commit()


def test_review_round_rejects_a_round_number_outside_the_allowed_range(
    session: Session,
) -> None:
    """The four-review cap is enforced even if a caller bypasses service logic."""
    session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
    job, candidate, review = _reviewed_job(session, CUSTOMER_A_ID)
    review_session = _review_session(CUSTOMER_A_ID, job.id)
    session.add(review_session)
    session.flush()
    session.add(
        _review_round(
            CUSTOMER_A_ID,
            review_session,
            job,
            candidate,
            review,
            round_number=5,
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_review_round_rejects_a_duplicate_round_number_for_one_session(
    session: Session,
) -> None:
    """One workflow may retain only one durable record for each numbered round."""
    session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
    job, candidate, review = _reviewed_job(session, CUSTOMER_A_ID)
    review_session = _review_session(CUSTOMER_A_ID, job.id)
    session.add(review_session)
    session.flush()
    session.add_all(
        [
            _review_round(CUSTOMER_A_ID, review_session, job, candidate, review),
            _review_round(CUSTOMER_A_ID, review_session, job, candidate, review),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_orm_rejects_review_round_update(session: Session) -> None:
    """ORM callers cannot rewrite a past customer-feedback round."""
    session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
    job, candidate, review = _reviewed_job(session, CUSTOMER_A_ID)
    review_session = _review_session(CUSTOMER_A_ID, job.id)
    session.add(review_session)
    session.flush()
    round_row = _review_round(CUSTOMER_A_ID, review_session, job, candidate, review)
    session.add(round_row)
    session.commit()
    round_row.round_number = 2

    with pytest.raises(DomainRuleError, match="image_review_round_immutable"):
        session.commit()


def test_orm_rejects_review_round_delete(session: Session) -> None:
    """ORM callers cannot erase a past customer-feedback round."""
    session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
    job, candidate, review = _reviewed_job(session, CUSTOMER_A_ID)
    review_session = _review_session(CUSTOMER_A_ID, job.id)
    session.add(review_session)
    session.flush()
    round_row = _review_round(CUSTOMER_A_ID, review_session, job, candidate, review)
    session.add(round_row)
    session.commit()
    session.delete(round_row)

    with pytest.raises(DomainRuleError, match="image_review_round_immutable"):
        session.flush()


def test_native_sql_rejects_review_round_update_and_delete(
    migrated_session: Session,
) -> None:
    """Migration triggers protect history even when callers bypass the ORM."""
    migrated_session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
    job, candidate, review = _reviewed_job(migrated_session, CUSTOMER_A_ID)
    review_session = _review_session(CUSTOMER_A_ID, job.id)
    migrated_session.add(review_session)
    migrated_session.flush()
    round_row = _review_round(CUSTOMER_A_ID, review_session, job, candidate, review)
    migrated_session.add(round_row)
    migrated_session.commit()

    with pytest.raises(IntegrityError, match="image_review_round_immutable"):
        migrated_session.execute(
            text("UPDATE image_review_rounds SET round_number = 2 WHERE id = :id"),
            {"id": str(round_row.id)},
        )
    migrated_session.rollback()

    with pytest.raises(IntegrityError, match="image_review_round_immutable"):
        migrated_session.execute(
            text("DELETE FROM image_review_rounds WHERE id = :id"),
            {"id": str(round_row.id)},
        )
    migrated_session.rollback()


def test_review_session_cas_allows_only_one_competing_claim(tmp_path: Path) -> None:
    """Two SQLite workers using the same revision cannot both claim one session."""
    engine = create_database_engine(f"sqlite:///{tmp_path / 'review-claim.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sessions() as setup_session:
        setup_session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        job = _job(CUSTOMER_A_ID)
        setup_session.add(job)
        setup_session.flush()
        review_session = _review_session(CUSTOMER_A_ID, job.id)
        setup_session.add(review_session)
        setup_session.commit()
        review_session_id = review_session.id

    with sessions() as first_worker, sessions() as second_worker:
        first_claim = ReviewSessionRepository(first_worker).claim_for_generation(
            CUSTOMER_A_ID, review_session_id, expected_revision=0
        )
        first_worker.commit()
        second_claim = ReviewSessionRepository(second_worker).claim_for_generation(
            CUSTOMER_A_ID, review_session_id, expected_revision=0
        )
        second_worker.commit()

    assert first_claim is not None
    assert first_claim.revision == 1
    assert second_claim is None


def _claim_store(tmp_path: Path) -> tuple[sessionmaker[Session], UUID, UUID]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'review-lease.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sessions() as setup_session:
        setup_session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        job = _job(CUSTOMER_A_ID)
        setup_session.add(job)
        setup_session.flush()
        review_session = _review_session(CUSTOMER_A_ID, job.id)
        setup_session.add(review_session)
        setup_session.commit()
        return sessions, review_session.id, job.id


def test_fresh_reader_cannot_claim_an_unexpired_generation_lease(
    tmp_path: Path,
) -> None:
    """A fresh revision cannot bypass another worker's unexpired claim."""
    sessions, review_session_id, _job_id = _claim_store(tmp_path)
    claimed_at = datetime(2026, 9, 7, tzinfo=UTC)
    with sessions() as owner:
        first_claim = ReviewSessionRepository(owner).claim_for_generation(
            CUSTOMER_A_ID,
            review_session_id,
            expected_revision=0,
            now=claimed_at,
            lease_duration=timedelta(minutes=5),
        )
        owner.commit()

    assert first_claim is not None

    with sessions() as fresh_reader:
        current = ReviewSessionRepository(fresh_reader).get_for_update(
            CUSTOMER_A_ID, review_session_id
        )
        assert current is not None
        assert current.revision == 1
        assert (
            ReviewSessionRepository(fresh_reader).claim_for_generation(
                CUSTOMER_A_ID,
                review_session_id,
                expected_revision=current.revision,
                now=claimed_at + timedelta(minutes=1),
                lease_duration=timedelta(minutes=5),
            )
            is None
        )


def test_wrong_owner_cannot_release_an_active_generation_claim(tmp_path: Path) -> None:
    """A worker token is required to release the persistent claim it owns."""
    sessions, review_session_id, _job_id = _claim_store(tmp_path)
    claimed_at = datetime(2026, 9, 7, tzinfo=UTC)
    with sessions() as owner:
        claim = ReviewSessionRepository(owner).claim_for_generation(
            CUSTOMER_A_ID,
            review_session_id,
            expected_revision=0,
            now=claimed_at,
            lease_duration=timedelta(minutes=5),
        )
        owner.commit()

    assert claim is not None
    with sessions() as wrong_worker:
        released = ReviewSessionRepository(wrong_worker).release_generation_claim(
            CUSTOMER_A_ID, review_session_id, owner_token="wrong-owner-token"
        )
        wrong_worker.commit()

    assert released is None
    with sessions() as reader:
        still_claimed = reader.get(ImageReviewSession, review_session_id)
        assert still_claimed is not None
        assert still_claimed.status == "regenerating"
        assert still_claimed.claim_owner_token == claim.claim_owner_token


def test_expired_generation_claim_can_be_recovered_by_a_new_worker(
    tmp_path: Path,
) -> None:
    """A crashed worker's expired lease cannot permanently block regeneration."""
    sessions, review_session_id, _job_id = _claim_store(tmp_path)
    claimed_at = datetime(2026, 9, 7, tzinfo=UTC)
    with sessions() as first_worker:
        first_claim = ReviewSessionRepository(first_worker).claim_for_generation(
            CUSTOMER_A_ID,
            review_session_id,
            expected_revision=0,
            now=claimed_at,
            lease_duration=timedelta(seconds=1),
        )
        first_worker.commit()

    assert first_claim is not None
    with sessions() as recovery_worker:
        recovered = ReviewSessionRepository(recovery_worker).claim_for_generation(
            CUSTOMER_A_ID,
            review_session_id,
            expected_revision=1,
            now=claimed_at + timedelta(seconds=2),
            lease_duration=timedelta(minutes=5),
        )
        recovery_worker.commit()

    assert recovered is not None
    assert recovered.revision == 2
    assert recovered.claim_owner_token != first_claim.claim_owner_token


def test_claim_owner_can_complete_generation_and_clear_the_lease(
    tmp_path: Path,
) -> None:
    """Only the owner can complete a claim and return the session to reviewable."""
    sessions, review_session_id, job_id = _claim_store(tmp_path)
    claimed_at = datetime(2026, 9, 7, tzinfo=UTC)
    with sessions() as owner:
        claim = ReviewSessionRepository(owner).claim_for_generation(
            CUSTOMER_A_ID,
            review_session_id,
            expected_revision=0,
            now=claimed_at,
            lease_duration=timedelta(minutes=5),
        )
        owner.commit()

    assert claim is not None
    assert claim.claim_owner_token is not None
    with sessions() as owner:
        completed = ReviewSessionRepository(owner).complete_generation_claim(
            CUSTOMER_A_ID,
            review_session_id,
            owner_token=claim.claim_owner_token,
            current_job_id=job_id,
        )
        owner.commit()

    assert completed is not None
    assert completed.status == "awaiting_review"
    assert completed.claim_owner_token is None
    assert completed.claimed_at is None
    assert completed.claim_expires_at is None


def test_downgrade_refuses_to_discard_review_sessions_with_customer_data(
    tmp_path: Path,
) -> None:
    """A downgrade cannot erase a customer's active review workflow."""
    database_url = f"sqlite:///{tmp_path / 'review-session-downgrade.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    now = datetime.now(UTC).isoformat()
    customer_id = str(CUSTOMER_A_ID)
    job_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Customer A', :now)"
            ),
            {"id": customer_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO image_output_jobs "
                "(id, customer_id, output_type, output_ratio, adapter_version, "
                "status, request_metadata, idempotency_key, created_at, updated_at) "
                "VALUES (:id, :customer_id, 'scene', '1:1', "
                "'deterministic-local-image-v1', 'queued', '{}', 'downgrade-job', "
                ":now, :now)"
            ),
            {"id": job_id, "customer_id": customer_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO image_review_sessions "
                "(id, customer_id, product_id, initial_handoff_id, current_job_id, initial_job_id, "
                "regeneration_count, status, revision, created_at, updated_at) "
                "VALUES (:id, :customer_id, 'product-a', :handoff_id, :job_id, :job_id, "
                "0, 'awaiting_review', 0, :now, :now)"
            ),
            {
                "id": str(uuid4()),
                "customer_id": customer_id,
                "handoff_id": str(uuid4()),
                "job_id": job_id,
                "now": now,
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="cannot discard customer image review"):
        command.downgrade(config, "0012_classification_bridge_claim_lease")
