"""Persist sealed suite fidelity selections and completed versions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_style_suite_fidelity"
down_revision: str | None = "0020_style_suite_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "style_suite_run_items",
        sa.Column("selected_candidate_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "style_suite_run_items",
        sa.Column("selected_review_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "style_suite_run_items",
        sa.Column("fidelity_state", sa.String(16), nullable=True),
    )
    op.add_column(
        "style_suite_run_items", sa.Column("fidelity_flags", sa.JSON(), nullable=True)
    )
    op.execute(
        "UPDATE style_suite_runs SET state='awaiting_review' WHERE state='completed'"
    )
    op.create_table(
        "style_suite_completions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "run_id"],
            ["style_suite_runs.customer_id", "style_suite_runs.id"],
        ),
        sa.UniqueConstraint("run_id", name="uq_style_suite_completion_run"),
        sa.CheckConstraint("version = 1", name="ck_style_suite_completion_version"),
    )
    op.create_index(
        "ix_style_suite_completions_customer_id",
        "style_suite_completions",
        ["customer_id"],
    )
    op.create_index(
        "ix_style_suite_completions_run_id", "style_suite_completions", ["run_id"]
    )
    op.create_table(
        "style_suite_candidate_fidelities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("run_item_id", sa.String(36), nullable=False),
        sa.Column("output_job_id", sa.String(36), nullable=False),
        sa.Column("image_candidate_id", sa.String(36), nullable=False),
        sa.Column("fidelity_state", sa.String(16), nullable=False),
        sa.Column("fidelity_flags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["run_item_id"], ["style_suite_run_items.id"]),
        sa.ForeignKeyConstraint(["output_job_id"], ["image_output_jobs.id"]),
        sa.ForeignKeyConstraint(["image_candidate_id"], ["image_candidates.id"]),
        sa.UniqueConstraint(
            "image_candidate_id", name="uq_style_suite_candidate_fidelity"
        ),
        sa.CheckConstraint(
            "fidelity_state IN ('green','red','unknown')",
            name="ck_style_suite_candidate_fidelity_state",
        ),
    )
    op.create_index(
        "ix_style_suite_candidate_fidelities_customer_id",
        "style_suite_candidate_fidelities",
        ["customer_id"],
    )
    op.create_index(
        "ix_style_suite_candidate_fidelities_run_item_id",
        "style_suite_candidate_fidelities",
        ["run_item_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_selection_lineage BEFORE UPDATE OF selected_candidate_id, selected_review_id ON style_suite_run_items "
        "WHEN NEW.selected_candidate_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM image_candidates c JOIN image_reviews v "
        "ON v.id=NEW.selected_review_id AND v.customer_id=NEW.customer_id AND v.image_candidate_id=c.id AND v.decision='approved' "
        "WHERE c.id=NEW.selected_candidate_id AND c.customer_id=NEW.customer_id AND c.output_job_id=NEW.output_job_id) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_selection_lineage_mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_selection_immutable BEFORE UPDATE ON style_suite_run_items "
        "WHEN (OLD.selected_candidate_id IS NOT NULL AND NEW.selected_candidate_id IS NOT OLD.selected_candidate_id) "
        "OR (OLD.selected_review_id IS NOT NULL AND NEW.selected_review_id IS NOT OLD.selected_review_id) "
        "OR (OLD.fidelity_state IS NOT NULL AND NEW.fidelity_state IS NOT OLD.fidelity_state) "
        "OR (OLD.fidelity_flags IS NOT NULL AND NEW.fidelity_flags IS NOT OLD.fidelity_flags) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_item_selection_immutable'); END"
    )
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
    op.execute(
        "CREATE TRIGGER trg_style_suite_candidate_fidelity_insert BEFORE INSERT ON style_suite_candidate_fidelities "
        "WHEN NOT EXISTS (SELECT 1 FROM style_suite_run_items i JOIN image_candidates c "
        "ON c.id=NEW.image_candidate_id AND c.customer_id=NEW.customer_id AND c.output_job_id=NEW.output_job_id "
        "WHERE i.id=NEW.run_item_id AND i.customer_id=NEW.customer_id AND i.output_job_id=NEW.output_job_id) "
        "OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN "
        "('subject_incomplete','component_count_changed','primary_color_changed','core_hardware_changed','product_form_changed','provider_check_unavailable')) "
        "OR (NEW.fidelity_state='green' AND json_array_length(NEW.fidelity_flags) != 0) "
        "OR (NEW.fidelity_state='red' AND json_array_length(NEW.fidelity_flags) = 0) "
        "OR (NEW.fidelity_state='unknown' AND (json_array_length(NEW.fidelity_flags) != 1 OR json_extract(NEW.fidelity_flags, '$[0]') != 'provider_check_unavailable')) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_candidate_fidelity_invalid'); END"
    )
    completion_requirements = (
        "NOT EXISTS (SELECT 1 FROM style_suite_run_items i WHERE i.run_id=NEW.id AND i.customer_id=NEW.customer_id) "
        "OR EXISTS (SELECT 1 FROM style_suite_run_items i WHERE i.run_id=NEW.id AND i.customer_id=NEW.customer_id AND "
        "(i.state!='approved' OR i.fidelity_state!='green' OR json_array_length(i.fidelity_flags)!=0 "
        "OR i.selected_candidate_id IS NULL OR i.selected_review_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM image_candidates c JOIN image_reviews v ON v.id=i.selected_review_id "
        "AND v.customer_id=i.customer_id AND v.image_candidate_id=c.id AND v.decision='approved' "
        "JOIN image_output_jobs j ON j.id=i.output_job_id AND j.customer_id=i.customer_id "
        "WHERE c.id=i.selected_candidate_id AND c.customer_id=i.customer_id "
        "AND c.output_job_id=i.output_job_id AND j.status='customer_approved')))"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_run_complete_guard BEFORE UPDATE OF state ON style_suite_runs "
        f"WHEN NEW.state='completed' AND ({completion_requirements}) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_completion_requirements_not_met'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_completion_insert BEFORE INSERT ON style_suite_completions "
        "WHEN NOT EXISTS (SELECT 1 FROM style_suite_runs r WHERE r.id=NEW.run_id AND r.customer_id=NEW.customer_id AND r.state='completed') "
        "OR COALESCE(json_extract(NEW.snapshot,'$.run_id'),'') != NEW.run_id "
        "OR COALESCE(json_extract(NEW.snapshot,'$.product_id'),'') != (SELECT product_id FROM style_suite_runs WHERE id=NEW.run_id) "
        "OR COALESCE(json_extract(NEW.snapshot,'$.source_asset_id'),'') != (SELECT source_asset_id FROM style_suite_runs WHERE id=NEW.run_id) "
        "OR COALESCE(json_extract(NEW.snapshot,'$.source_content_sha256'),'') != (SELECT source_content_sha256 FROM style_suite_runs WHERE id=NEW.run_id) "
        "OR COALESCE(json_array_length(NEW.snapshot,'$.items'),-1) != (SELECT count(*) FROM style_suite_run_items i WHERE i.run_id=NEW.run_id) "
        "OR EXISTS (SELECT 1 FROM style_suite_run_items i WHERE i.run_id=NEW.run_id AND NOT EXISTS "
        "(SELECT 1 FROM json_each(NEW.snapshot,'$.items') s WHERE json_extract(s.value,'$.item_id')=i.id "
        "AND json_extract(s.value,'$.style_version_id')=i.scene_style_version_id AND json_extract(s.value,'$.slot_key')=i.slot_key "
        "AND json_extract(s.value,'$.selected_candidate_id')=i.selected_candidate_id "
        "AND json_extract(s.value,'$.selected_review_id')=i.selected_review_id "
        "AND json_extract(s.value,'$.fidelity_state')=i.fidelity_state "
        "AND json(json_extract(s.value,'$.fidelity_flags'))=json(i.fidelity_flags))) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_completion_snapshot_invalid'); END"
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_style_suite_completion_no_{operation.lower()} BEFORE {operation} ON style_suite_completions "
            "BEGIN SELECT RAISE(ABORT, 'style_suite_completion_immutable'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_style_suite_candidate_fidelity_no_{operation.lower()} BEFORE {operation} ON style_suite_candidate_fidelities "
            "BEGIN SELECT RAISE(ABORT, 'style_suite_candidate_fidelity_immutable'); END"
        )


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM style_suite_completions"))
        .scalar_one()
    )
    fidelity_count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM style_suite_candidate_fidelities"))
        .scalar_one()
    )
    selection_count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM style_suite_run_items WHERE selected_candidate_id IS NOT NULL "
                "OR selected_review_id IS NOT NULL OR fidelity_state IS NOT NULL OR fidelity_flags IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if count or fidelity_count or selection_count:
        raise RuntimeError("cannot discard style suite review evidence")
    for name in (
        "trg_style_suite_completion_no_update",
        "trg_style_suite_completion_no_delete",
        "trg_style_suite_fidelity_bounded",
        "trg_style_suite_selection_immutable",
        "trg_style_suite_selection_lineage",
        "trg_style_suite_candidate_fidelity_no_update",
        "trg_style_suite_candidate_fidelity_no_delete",
        "trg_style_suite_candidate_fidelity_insert",
        "trg_style_suite_run_complete_guard",
        "trg_style_suite_completion_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index(
        "ix_style_suite_completions_run_id", table_name="style_suite_completions"
    )
    op.drop_index(
        "ix_style_suite_completions_customer_id", table_name="style_suite_completions"
    )
    op.drop_table("style_suite_completions")
    op.drop_index(
        "ix_style_suite_candidate_fidelities_run_item_id",
        table_name="style_suite_candidate_fidelities",
    )
    op.drop_index(
        "ix_style_suite_candidate_fidelities_customer_id",
        table_name="style_suite_candidate_fidelities",
    )
    op.drop_table("style_suite_candidate_fidelities")
    for column in (
        "fidelity_flags",
        "fidelity_state",
        "selected_review_id",
        "selected_candidate_id",
    ):
        op.drop_column("style_suite_run_items", column)
