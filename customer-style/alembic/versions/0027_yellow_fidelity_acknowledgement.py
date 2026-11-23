"""Add acknowledged yellow fidelity decisions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_yellow_fidelity_acknowledgement"
down_revision: str | None = "0026_real_style_analysis_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("style_suite_candidate_fidelities") as batch:
        batch.add_column(
            sa.Column(
                "warning_acknowledged",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.drop_constraint(
            "ck_style_suite_candidate_fidelity_state", type_="check"
        )
        batch.create_check_constraint(
            "ck_style_suite_candidate_fidelity_state",
            "fidelity_state IN ('green','yellow','red','unknown')",
        )
    _install_candidate_fidelity_triggers(yellow=True)
    op.execute("DROP TRIGGER trg_style_suite_fidelity_bounded")
    op.execute(
        "CREATE TRIGGER trg_style_suite_fidelity_bounded BEFORE UPDATE OF fidelity_state, fidelity_flags ON style_suite_run_items "
        "WHEN NEW.fidelity_state NOT IN ('green','yellow','red','unknown') "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN "
        "('subject_incomplete','component_count_changed','primary_color_changed','core_hardware_changed','product_form_changed','provider_check_unavailable','minor_color_drift','minor_detail_uncertain','background_style_drift')) "
        "OR (NEW.fidelity_state='green' AND json_array_length(NEW.fidelity_flags) != 0) "
        "OR (NEW.fidelity_state='yellow' AND (json_array_length(NEW.fidelity_flags) = 0 OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN ('minor_color_drift','minor_detail_uncertain','background_style_drift')))) "
        "OR (NEW.fidelity_state='red' AND json_array_length(NEW.fidelity_flags) = 0) "
        "OR (NEW.fidelity_state='unknown' AND (json_array_length(NEW.fidelity_flags) != 1 OR json_extract(NEW.fidelity_flags, '$[0]') != 'provider_check_unavailable')) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_fidelity_invalid'); END"
    )
    op.execute("DROP TRIGGER trg_style_suite_run_complete_guard")
    _install_run_complete_guard(yellow=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT count(*) FROM style_suite_candidate_fidelities")
    ).scalar_one():
        raise RuntimeError("cannot discard style suite review evidence")
    if bind.execute(
        sa.text(
            "SELECT count(*) FROM style_suite_candidate_fidelities "
            "WHERE fidelity_state = 'yellow'"
        )
    ).scalar_one():
        raise RuntimeError("cannot discard acknowledged yellow fidelity evidence")
    with op.batch_alter_table("style_suite_candidate_fidelities") as batch:
        batch.drop_constraint(
            "ck_style_suite_candidate_fidelity_state", type_="check"
        )
        batch.create_check_constraint(
            "ck_style_suite_candidate_fidelity_state",
            "fidelity_state IN ('green','red','unknown')",
        )
        batch.drop_column("warning_acknowledged")
    _install_candidate_fidelity_triggers(yellow=False)
    op.execute("DROP TRIGGER trg_style_suite_fidelity_bounded")
    op.execute(
        "CREATE TRIGGER trg_style_suite_fidelity_bounded BEFORE UPDATE OF fidelity_state, fidelity_flags ON style_suite_run_items "
        "WHEN NEW.fidelity_state NOT IN ('green','red','unknown') "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN "
        "('subject_incomplete','component_count_changed','primary_color_changed','core_hardware_changed','product_form_changed','provider_check_unavailable')) "
        "OR (NEW.fidelity_state='green' AND json_array_length(NEW.fidelity_flags) != 0) "
        "OR (NEW.fidelity_state='red' AND json_array_length(NEW.fidelity_flags) = 0) "
        "OR (NEW.fidelity_state='unknown' AND (json_array_length(NEW.fidelity_flags) != 1 OR json_extract(NEW.fidelity_flags, '$[0]') != 'provider_check_unavailable')) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_fidelity_invalid'); END"
    )
    op.execute("DROP TRIGGER trg_style_suite_run_complete_guard")
    _install_run_complete_guard(yellow=False)


def _install_candidate_fidelity_triggers(*, yellow: bool) -> None:
    allowed = (
        "'subject_incomplete','component_count_changed','primary_color_changed',"
        "'core_hardware_changed','product_form_changed','provider_check_unavailable'"
    )
    yellow_rules = ""
    if yellow:
        allowed += ",'minor_color_drift','minor_detail_uncertain','background_style_drift'"
        yellow_rules = (
            "OR (NEW.fidelity_state='yellow' AND "
            "(NEW.warning_acknowledged!=1 OR json_array_length(NEW.fidelity_flags)=0 "
            "OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN "
            "('minor_color_drift','minor_detail_uncertain','background_style_drift')))) "
        )
    op.execute(
        "CREATE TRIGGER trg_style_suite_candidate_fidelity_insert BEFORE INSERT ON style_suite_candidate_fidelities "
        "WHEN NOT EXISTS (SELECT 1 FROM style_suite_run_items i JOIN image_candidates c "
        "ON c.id=NEW.image_candidate_id AND c.customer_id=NEW.customer_id AND c.output_job_id=NEW.output_job_id "
        "WHERE i.id=NEW.run_item_id AND i.customer_id=NEW.customer_id AND i.output_job_id=NEW.output_job_id) "
        f"OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN ({allowed})) "
        "OR (NEW.fidelity_state='green' AND json_array_length(NEW.fidelity_flags) != 0) "
        f"{yellow_rules}"
        "OR (NEW.fidelity_state='red' AND json_array_length(NEW.fidelity_flags) = 0) "
        "OR (NEW.fidelity_state='unknown' AND (json_array_length(NEW.fidelity_flags) != 1 OR json_extract(NEW.fidelity_flags, '$[0]') != 'provider_check_unavailable')) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_candidate_fidelity_invalid'); END"
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_style_suite_candidate_fidelity_no_{operation.lower()} BEFORE {operation} ON style_suite_candidate_fidelities "
            "BEGIN SELECT RAISE(ABORT, 'style_suite_candidate_fidelity_immutable'); END"
        )


def _install_run_complete_guard(*, yellow: bool) -> None:
    accepted = (
        "(i.fidelity_state!='green' OR json_array_length(i.fidelity_flags)!=0)"
        if not yellow
        else "((i.fidelity_state='green' AND json_array_length(i.fidelity_flags)!=0) OR i.fidelity_state NOT IN ('green','yellow'))"
    )
    requirements = (
        "NOT EXISTS (SELECT 1 FROM style_suite_run_items i WHERE i.run_id=NEW.id AND i.customer_id=NEW.customer_id) "
        "OR EXISTS (SELECT 1 FROM style_suite_run_items i WHERE i.run_id=NEW.id AND i.customer_id=NEW.customer_id AND "
        f"(i.state!='approved' OR {accepted} OR i.selected_candidate_id IS NULL OR i.selected_review_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM image_candidates c JOIN image_reviews v ON v.id=i.selected_review_id "
        "AND v.customer_id=i.customer_id AND v.image_candidate_id=c.id AND v.decision='approved' "
        "JOIN image_output_jobs j ON j.id=i.output_job_id AND j.customer_id=i.customer_id "
        "WHERE c.id=i.selected_candidate_id AND c.customer_id=i.customer_id "
        "AND c.output_job_id=i.output_job_id AND j.status='customer_approved')))"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_run_complete_guard BEFORE UPDATE OF state ON style_suite_runs "
        f"WHEN NEW.state='completed' AND ({requirements}) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_completion_requirements_not_met'); END"
    )
