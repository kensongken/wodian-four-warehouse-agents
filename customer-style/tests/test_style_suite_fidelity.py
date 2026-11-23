"""Suite slots complete only after bounded fidelity and customer selection."""
# ruff: noqa: F401, F811

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from test_classification_bridge_api import _real_generator
from test_image_jobs_api import (
    CUSTOMER_A_ID,
    api_session_factory,
    asset_factory,
    content_store,
)
from test_product_style_output import _confirmed_identity_card, _historical

from alembic import command
from opc_style.db import create_database_engine
from opc_style.domain.authenticity import ALLOWED_FIDELITY_FLAGS
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import DeterministicLocalImageGenerator
from opc_style.models import (
    ImageCandidate,
    StyleSuiteCandidateFidelity,
    StyleSuiteCompletion,
    StyleSuiteRun,
    StyleSuiteRunItem,
)
from opc_style.services.image_reviews import (
    ImageReviewConflictError,
    ImageReviewService,
)
from opc_style.services.style_suites import StyleSuiteService
from opc_style.settings import StyleSuitePlanningSettings


def _generated_suite(
    session: Any, store: Any, asset: Any, *, real_model: bool = False
) -> Any:
    _historical(session, store, name="fidelity-style")
    card = _confirmed_identity_card(session, asset) if real_model else None
    run = StyleSuiteService(
        session,
        settings=StyleSuitePlanningSettings(real_generation_enabled=True),
        content_store=store,
        generator=(
            _real_generator([])
            if real_model
            else DeterministicLocalImageGenerator()
        ),
        allow_synthetic_product_style=not real_model,
    ).create(
        CUSTOMER_A_ID,
        asset.product_id,
        asset.id,
        idempotency_key="fidelity-suite",
        product_identity_card_version_id=card.id if card is not None else None,
    )
    return run


def _candidate_and_item(session: Any, run: Any) -> tuple[Any, Any]:
    item = session.scalar(
        select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
    )
    assert item is not None and item.output_job_id is not None
    candidate = session.scalar(
        select(ImageCandidate).where(
            ImageCandidate.output_job_id == item.output_job_id
        )
    )
    assert candidate is not None
    return candidate, item


def test_real_candidate_cannot_forge_persisted_red_as_green(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset, real_model=True)
        candidate, item = _candidate_and_item(session, run)
        session.add(
            StyleSuiteCandidateFidelity(
                customer_id=CUSTOMER_A_ID,
                run_item_id=item.id,
                output_job_id=item.output_job_id,
                image_candidate_id=candidate.id,
                fidelity_state="red",
                fidelity_flags=["product_category_changed"],
                summary_zh="商品类别发生变化",
                warning_acknowledged=False,
            )
        )
        session.commit()

        with pytest.raises(
            ImageReviewConflictError, match="candidate_fidelity_rejected"
        ):
            ImageReviewService(session, content_store).decide(
                CUSTOMER_A_ID,
                item.output_job_id,
                candidate.id,
                decision="approved",
                reason=None,
                idempotency_key="forged-green",
                fidelity_state="green",
                fidelity_flags=(),
            )


def test_real_candidate_requires_server_owned_fidelity_before_review(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset, real_model=True)
        candidate, item = _candidate_and_item(session, run)

        with pytest.raises(
            ImageReviewConflictError, match="candidate_fidelity_required"
        ):
            ImageReviewService(session, content_store).decide(
                CUSTOMER_A_ID,
                item.output_job_id,
                candidate.id,
                decision="approved",
                reason=None,
                idempotency_key="no-machine-fidelity",
                fidelity_state="green",
                fidelity_flags=(),
            )


def test_real_yellow_uses_machine_result_and_records_one_way_acknowledgement(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset, real_model=True)
        candidate, item = _candidate_and_item(session, run)
        evidence = StyleSuiteCandidateFidelity(
            customer_id=CUSTOMER_A_ID,
            run_item_id=item.id,
            output_job_id=item.output_job_id,
            image_candidate_id=candidate.id,
            fidelity_state="yellow",
            fidelity_flags=["minor_color_drift"],
            summary_zh="存在轻微色差",
            warning_acknowledged=False,
        )
        session.add(evidence)
        session.commit()

        result = ImageReviewService(session, content_store).decide(
            CUSTOMER_A_ID,
            item.output_job_id,
            candidate.id,
            decision="approved",
            reason="我已人工确认并接受轻微色差",
            idempotency_key="machine-yellow-ack",
            fidelity_warning_acknowledged=True,
        )

        session.refresh(evidence)
        assert result.suite_state == "completed"
        assert evidence.warning_acknowledged is True


def test_only_controlled_fidelity_flags_are_part_of_the_contract() -> None:
    assert ALLOWED_FIDELITY_FLAGS == frozenset(
        {
            "subject_incomplete",
            "component_count_changed",
            "primary_color_changed",
            "core_hardware_changed",
            "product_form_changed",
            "product_category_changed",
            "component_shape_changed",
            "component_arrangement_changed",
            "core_accessory_changed",
            "provider_check_unavailable",
            "minor_color_drift",
            "minor_detail_uncertain",
            "background_style_drift",
        }
    )


def test_yellow_candidate_requires_warning_acknowledgement_then_completes(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        )
        assert item is not None and item.output_job_id is not None
        candidate = session.scalar(
            select(ImageCandidate).where(
                ImageCandidate.output_job_id == item.output_job_id
            )
        )
        assert candidate is not None
        with pytest.raises(
            ImageReviewConflictError,
            match="suite_candidate_fidelity_warning_acknowledgement_required",
        ):
            ImageReviewService(session, content_store).decide(
                CUSTOMER_A_ID,
                item.output_job_id,
                candidate.id,
                decision="approved",
                reason="颜色存在轻微偏差",
                idempotency_key="yellow-without-ack",
                fidelity_state="yellow",
                fidelity_flags=("minor_color_drift",),
            )
        session.rollback()
        outcome = ImageReviewService(session, content_store).decide(
            CUSTOMER_A_ID,
            item.output_job_id,
            candidate.id,
            decision="approved",
            reason="颜色存在轻微偏差，已人工接受",
            idempotency_key="yellow-with-ack",
            fidelity_state="yellow",
            fidelity_flags=("minor_color_drift",),
            fidelity_warning_acknowledged=True,
        )
        assert outcome.suite_state == "completed"
        evidence = session.query(StyleSuiteCandidateFidelity).one()
        assert evidence.warning_acknowledged is True


@pytest.mark.parametrize("state", ["red", "unknown"])
def test_non_green_candidate_cannot_fill_suite_slot(
    state: str, api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        )
        assert item is not None and item.output_job_id is not None, (
            item.failure_reason if item else None
        )
        candidate = session.scalar(
            select(ImageCandidate).where(
                ImageCandidate.output_job_id == item.output_job_id
            )
        )
        assert candidate is not None
        session.expire_all()
        with pytest.raises(
            ImageReviewConflictError, match="suite_candidate_fidelity_not_green"
        ):
            ImageReviewService(session, content_store).decide(
                CUSTOMER_A_ID,
                item.output_job_id,
                candidate.id,
                decision="approved",
                reason=None,
                idempotency_key=f"reject-{state}",
                fidelity_state=state,
                fidelity_flags=("provider_check_unavailable",)
                if state == "unknown"
                else ("product_form_changed",),
            )
        session.refresh(item)
        assert item.selected_candidate_id is None
        assert session.query(StyleSuiteCompletion).count() == 0


def test_green_customer_approval_completes_suite_as_immutable_version(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        )
        assert item is not None and item.output_job_id is not None
        candidate = list(
            session.scalars(
                select(ImageCandidate).where(
                    ImageCandidate.output_job_id == item.output_job_id
                )
            )
        )[1]
        session.expire_all()
        result = ImageReviewService(session, content_store).decide(
            CUSTOMER_A_ID,
            item.output_job_id,
            candidate.id,
            decision="approved",
            reason=None,
            idempotency_key="approve-green",
            fidelity_state="green",
            fidelity_flags=(),
        )
        session.refresh(item)
        assert result.suite_state == "completed"
        assert item.selected_candidate_id == candidate.id
        assert item.fidelity_state == "green"
        completion = session.query(StyleSuiteCompletion).one()
        assert completion.version == 1
        assert completion.snapshot["items"][0]["selected_candidate_id"] == str(
            candidate.id
        )
        completion.version = 2
        with pytest.raises(DomainRuleError, match="style_suite_completion_immutable"):
            session.commit()


def test_rejected_red_candidate_is_recorded_but_another_green_candidate_can_fill_slot(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        )
        assert item is not None and item.output_job_id is not None
        candidates = list(
            session.scalars(
                select(ImageCandidate).where(
                    ImageCandidate.output_job_id == item.output_job_id
                )
            )
        )
        rejected = ImageReviewService(session, content_store).decide(
            CUSTOMER_A_ID,
            item.output_job_id,
            candidates[0].id,
            decision="rejected",
            reason=None,
            idempotency_key="red-first",
            fidelity_state="red",
            fidelity_flags=("product_form_changed",),
        )
        assert rejected.job.status == "awaiting_customer_review"
        assert item.selected_candidate_id is None
        fidelity = session.query(StyleSuiteCandidateFidelity).one()
        assert fidelity.fidelity_state == "red"
        replay = ImageReviewService(session, content_store).decide(
            CUSTOMER_A_ID,
            item.output_job_id,
            candidates[0].id,
            decision="rejected",
            reason=None,
            idempotency_key="red-first",
            fidelity_state="red",
            fidelity_flags=("product_form_changed",),
        )
        assert replay.created is False
        approved = ImageReviewService(session, content_store).decide(
            CUSTOMER_A_ID,
            item.output_job_id,
            candidates[1].id,
            decision="approved",
            reason=None,
            idempotency_key="green-second",
            fidelity_state="green",
            fidelity_flags=(),
        )
        assert approved.suite_state == "completed"
        assert item.selected_candidate_id == candidates[1].id


def test_concurrent_same_approval_creates_one_completion_version(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        )
        assert item is not None and item.output_job_id is not None
        candidate = session.scalar(
            select(ImageCandidate).where(
                ImageCandidate.output_job_id == item.output_job_id
            )
        )
        assert candidate is not None
        job_id, candidate_id = item.output_job_id, candidate.id

    def approve() -> bool:
        with api_session_factory() as session:
            return (
                ImageReviewService(session, content_store)
                .decide(
                    CUSTOMER_A_ID,
                    job_id,
                    candidate_id,
                    decision="approved",
                    reason=None,
                    idempotency_key="concurrent-green",
                    fidelity_state="green",
                    fidelity_flags=(),
                )
                .created
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: approve(), range(2)))
    assert sorted(results) == [False, True]
    with api_session_factory() as session:
        assert session.query(StyleSuiteCompletion).count() == 1
        assert session.query(StyleSuiteCompletion).one().version == 1


def test_same_run_cannot_have_a_second_completion_via_orm_or_raw_sql(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        )
        assert item is not None and item.output_job_id is not None
        candidate = session.scalar(
            select(ImageCandidate).where(
                ImageCandidate.output_job_id == item.output_job_id
            )
        )
        assert candidate is not None
        ImageReviewService(session, content_store).decide(
            CUSTOMER_A_ID,
            item.output_job_id,
            candidate.id,
            decision="approved",
            reason=None,
            idempotency_key="first-and-only-completion",
            fidelity_state="green",
            fidelity_flags=(),
        )
        completion = session.query(StyleSuiteCompletion).one()
        session.add(
            StyleSuiteCompletion(
                customer_id=CUSTOMER_A_ID,
                run_id=run.id,
                version=2,
                snapshot=completion.snapshot,
            )
        )
        with pytest.raises((DomainRuleError, IntegrityError)):
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO style_suite_completions "
                    "(id,customer_id,run_id,version,snapshot,created_at) "
                    "VALUES (:id,:customer,:run,2,:snapshot,:now)"
                ),
                {
                    "id": str(__import__("uuid").uuid4()),
                    "customer": str(CUSTOMER_A_ID),
                    "run": str(run.id),
                    "snapshot": json.dumps(completion.snapshot),
                    "now": "2026-09-08T00:00:00+00:00",
                },
            )
            session.commit()


def test_concurrent_last_two_slots_leave_exactly_one_completed_version(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        _historical(session, content_store, name="concurrent-style-a")
        _historical(session, content_store, name="concurrent-style-b")
        run = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=DeterministicLocalImageGenerator(),
            allow_synthetic_product_style=True,
        ).create(
            CUSTOMER_A_ID,
            asset.product_id,
            asset.id,
            idempotency_key="concurrent-last-two",
        )
        work = []
        for item in session.scalars(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        ):
            assert item.output_job_id is not None
            candidate = session.scalar(
                select(ImageCandidate).where(
                    ImageCandidate.output_job_id == item.output_job_id
                )
            )
            assert candidate is not None
            work.append((item.output_job_id, candidate.id))
        assert len(work) == 2
        run_id = run.id

    def approve(pair: tuple[Any, Any]) -> str | None:
        job_id, candidate_id = pair
        with api_session_factory() as session:
            return ImageReviewService(session, content_store).decide(
                CUSTOMER_A_ID,
                job_id,
                candidate_id,
                decision="approved",
                reason=None,
                idempotency_key=f"approve-{job_id}",
                fidelity_state="green",
                fidelity_flags=(),
            ).suite_state

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(approve, work))
    with api_session_factory() as session:
        run = session.get(StyleSuiteRun, run_id)
        assert run is not None and run.state == "completed"
        completions = session.scalars(
            select(StyleSuiteCompletion).where(
                StyleSuiteCompletion.run_id == run_id
            )
        ).all()
        assert len(completions) == 1
        assert completions[0].version == 1


def test_reconciliation_suite_candidate_cannot_be_approved(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        item = session.scalar(
            select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
        )
        assert item is not None and item.output_job_id is not None
        candidate = session.scalar(
            select(ImageCandidate).where(
                ImageCandidate.output_job_id == item.output_job_id
            )
        )
        assert candidate is not None
        session.expire_all()
        item.state = "uncertain"
        session.commit()
        with pytest.raises(
            ImageReviewConflictError, match="suite_item_needs_reconciliation"
        ):
            ImageReviewService(session, content_store).decide(
                CUSTOMER_A_ID,
                item.output_job_id,
                candidate.id,
                decision="approved",
                reason=None,
                idempotency_key="uncertain-approve",
                fidelity_state="green",
                fidelity_flags=(),
            )
        run_row = session.get(StyleSuiteRun, run.id)
        assert run_row is not None
        run_row.state = "completed"
        with pytest.raises(
            DomainRuleError, match="style_suite_completion_requirements_not_met"
        ):
            session.commit()


def test_database_rejects_cross_job_candidate_selection(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        _historical(session, content_store, name="one")
        _historical(session, content_store, name="two")
        run = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=content_store,
            generator=DeterministicLocalImageGenerator(),
            allow_synthetic_product_style=True,
        ).create(CUSTOMER_A_ID, asset.product_id, asset.id, idempotency_key="cross-job")
        items = list(
            session.scalars(
                select(StyleSuiteRunItem).where(StyleSuiteRunItem.run_id == run.id)
            )
        )
        foreign_candidate = session.scalar(
            select(ImageCandidate).where(
                ImageCandidate.output_job_id == items[1].output_job_id
            )
        )
        assert foreign_candidate is not None
        items[0].selected_candidate_id = foreign_candidate.id
        with pytest.raises(
            DomainRuleError, match="style_suite_selection_lineage_mismatch"
        ):
            session.commit()


def test_0021_migration_installs_fidelity_lineage_and_immutability_guards(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'suite-fidelity.db'}"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {
        "style_suite_completions",
        "style_suite_candidate_fidelities",
    } <= set(inspector.get_table_names())
    columns = {
        column["name"] for column in inspector.get_columns("style_suite_run_items")
    }
    assert {
        "selected_candidate_id",
        "selected_review_id",
        "fidelity_state",
        "fidelity_flags",
    } <= columns
    assert any(
        constraint["column_names"] == ["run_id"]
        for constraint in inspector.get_unique_constraints(
            "style_suite_completions"
        )
    )
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
    assert {
        "trg_style_suite_selection_lineage",
        "trg_style_suite_selection_immutable",
        "trg_style_suite_candidate_fidelity_insert",
        "trg_style_suite_candidate_fidelity_no_update",
        "trg_style_suite_completion_no_update",
    } <= triggers
    engine.dispose()


def test_0021_sql_guards_reject_naked_completed_and_forged_snapshot(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'suite-forgery.db'}"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    run_id = str(__import__("uuid").uuid4())
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text("DROP TRIGGER trg_style_suite_runs_source"))
        connection.execute(
            text(
                "INSERT INTO style_suite_runs (id,customer_id,product_id,source_asset_id,source_content_sha256,style_count,slot_count,child_job_count,candidate_count,candidate_estimate_usd,maximum_estimated_cost_usd,cost_basis,currency,real_generation_enabled,idempotency_key,state,created_at) VALUES (:id,:customer,'p',:asset,:sha,1,1,1,3,0.1,0.3,'local_planning_estimate','USD',0,'sql-forge','awaiting_review',:now)"
            ),
            {
                "id": run_id,
                "customer": str(CUSTOMER_A_ID),
                "asset": str(__import__("uuid").uuid4()),
                "sha": "a" * 64,
                "now": "2026-09-08T00:00:00+00:00",
            },
        )
        with pytest.raises(
            IntegrityError, match="style_suite_completion_requirements_not_met"
        ):
            connection.execute(
                text("UPDATE style_suite_runs SET state='completed' WHERE id=:id"),
                {"id": run_id},
            )
        with pytest.raises(
            IntegrityError, match="style_suite_completion_snapshot_invalid"
        ):
            connection.execute(
                text(
                    "INSERT INTO style_suite_completions (id,customer_id,run_id,version,snapshot,created_at) VALUES (:id,:customer,:run,1,:snapshot,:now)"
                ),
                {
                    "id": str(__import__("uuid").uuid4()),
                    "customer": str(CUSTOMER_A_ID),
                    "run": run_id,
                    "snapshot": '{"run_id":"wrong","items":[]}',
                    "now": "2026-09-08T00:00:00+00:00",
                },
            )
    engine.dispose()


def test_orm_and_sql_cannot_forge_completed_run_or_snapshot(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        run_row = session.get(StyleSuiteRun, run.id)
        assert run_row is not None
        run_row.state = "completed"
        with pytest.raises(
            DomainRuleError, match="style_suite_completion_requirements_not_met"
        ):
            session.commit()


def test_response_does_not_trust_naked_completed_state(
    api_session_factory: Any, content_store: Any, asset_factory: Any
) -> None:
    asset = asset_factory()
    with api_session_factory() as session:
        run = _generated_suite(session, content_store, asset)
        session.execute(
            text("UPDATE style_suite_runs SET state='completed' WHERE id=:id"),
            {"id": str(run.id)},
        )
        session.commit()
        response = StyleSuiteService(session).get(CUSTOMER_A_ID, run.id)
        assert response.state == "awaiting_review"
        session.rollback()
        fake = StyleSuiteCompletion(
            customer_id=CUSTOMER_A_ID,
            run_id=run.id,
            version=1,
            snapshot={"items": []},
        )
        session.add(fake)
        with pytest.raises(
            DomainRuleError, match="style_suite_completion_snapshot_invalid"
        ):
            session.commit()


def test_0021_upgrade_demotes_generation_only_completed_multi_item_run(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'legacy-completed-suite.db'}"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0020_style_suite_execution")
    engine = create_database_engine(database_url)
    run_id = str(__import__("uuid").uuid4())
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        for trigger in (
            "trg_style_suite_runs_source",
            "trg_style_suite_items_limit",
            "trg_style_suite_items_snapshot",
        ):
            connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
        connection.execute(
            text(
                "INSERT INTO style_suite_runs (id,customer_id,product_id,source_asset_id,source_content_sha256,style_count,slot_count,child_job_count,candidate_count,candidate_estimate_usd,maximum_estimated_cost_usd,cost_basis,currency,real_generation_enabled,idempotency_key,state,created_at) VALUES (:id,:customer,'p',:asset,:sha,2,2,2,6,0.1,0.6,'local_planning_estimate','USD',0,'legacy-completed','completed',:now)"
            ),
            {
                "id": run_id,
                "customer": str(CUSTOMER_A_ID),
                "asset": str(__import__("uuid").uuid4()),
                "sha": "a" * 64,
                "now": "2026-09-08T00:00:00+00:00",
            },
        )
        for position in range(2):
            connection.execute(
                text(
                    "INSERT INTO style_suite_run_items (id,customer_id,run_id,scene_style_id,scene_style_version_id,style_name,style_version,slot_id,slot_key,style_snapshot,slot_snapshot,output_job_id,failure_reason,state,created_at) VALUES (:id,:customer,:run,:scene,:version,:name,1,:slot,:key,'{}','{}',NULL,NULL,'succeeded',:now)"
                ),
                {
                    "id": str(__import__("uuid").uuid4()),
                    "customer": str(CUSTOMER_A_ID),
                    "run": run_id,
                    "scene": str(__import__("uuid").uuid4()),
                    "version": str(__import__("uuid").uuid4()),
                    "name": f"style-{position}",
                    "slot": str(__import__("uuid").uuid4()),
                    "key": f"slot-{position}",
                    "now": "2026-09-08T00:00:00+00:00",
                },
            )
    engine.dispose()
    command.upgrade(config, "0021_style_suite_fidelity")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT state FROM style_suite_runs WHERE id=:id"), {"id": run_id}
            ).scalar_one()
            == "awaiting_review"
        )
    engine.dispose()


def test_0021_downgrade_refuses_any_fidelity_evidence(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'fidelity-downgrade.db'}"
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text("DROP TRIGGER trg_style_suite_candidate_fidelity_insert")
        )
        connection.execute(
            text(
                "INSERT INTO style_suite_candidate_fidelities (id,customer_id,run_item_id,output_job_id,image_candidate_id,fidelity_state,fidelity_flags,created_at) VALUES (:id,:customer,:item,:job,:candidate,'red','[\"product_form_changed\"]',:now)"
            ),
            {
                "id": str(__import__("uuid").uuid4()),
                "customer": str(CUSTOMER_A_ID),
                "item": str(__import__("uuid").uuid4()),
                "job": str(__import__("uuid").uuid4()),
                "candidate": str(__import__("uuid").uuid4()),
                "now": "2026-09-08T00:00:00+00:00",
            },
        )
    engine.dispose()
    with pytest.raises(
        RuntimeError, match="cannot discard style suite review evidence"
    ):
        command.downgrade(config, "0020_style_suite_execution")
