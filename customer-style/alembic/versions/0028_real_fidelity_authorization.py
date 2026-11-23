"""Add single-dispatch fidelity authorization and strict identity flags."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_real_fidelity_authorization"
down_revision: str | None = "0027_yellow_fidelity_acknowledgement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "real_fidelity_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("style_suite_run_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("authorized_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("dispatch_state", sa.String(32), nullable=False),
        sa.Column("outbound_attempts", sa.Integer(), nullable=False),
        sa.Column("assessment_json", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["style_suite_run_id"], ["style_suite_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_real_fidelity_customer_key"
        ),
        sa.CheckConstraint(
            "authorized_usd > 0", name="ck_real_fidelity_usd_positive"
        ),
        sa.CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_real_fidelity_dispatch_state",
        ),
        sa.CheckConstraint(
            "outbound_attempts IN (0, 1)", name="ck_real_fidelity_one_attempt"
        ),
    )
    op.create_index(
        "ix_real_fidelity_customer", "real_fidelity_runs", ["customer_id"]
    )
    op.add_column(
        "style_suite_candidate_fidelities",
        sa.Column("summary_zh", sa.String(200), nullable=True),
    )
    _install_ledger_guards()
    _replace_fidelity_guards(expanded=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT count(*) FROM real_fidelity_runs")
    ).scalar_one():
        raise RuntimeError("cannot discard paid fidelity evidence")
    _replace_fidelity_guards(expanded=False)
    op.drop_column("style_suite_candidate_fidelities", "summary_zh")
    op.execute("DROP TRIGGER IF EXISTS trg_real_fidelity_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_real_fidelity_scope_immutable")
    op.drop_index("ix_real_fidelity_customer", table_name="real_fidelity_runs")
    op.drop_table("real_fidelity_runs")


def _install_ledger_guards() -> None:
    op.execute(
        "CREATE TRIGGER trg_real_fidelity_scope_immutable BEFORE UPDATE ON real_fidelity_runs "
        "WHEN OLD.customer_id != NEW.customer_id "
        "OR OLD.style_suite_run_id != NEW.style_suite_run_id "
        "OR OLD.idempotency_key != NEW.idempotency_key "
        "OR OLD.model != NEW.model OR OLD.authorized_usd != NEW.authorized_usd "
        "OR OLD.input_manifest_sha256 != NEW.input_manifest_sha256 "
        "OR OLD.input_manifest != NEW.input_manifest "
        "BEGIN SELECT RAISE(ABORT, 'real_fidelity_authorization_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_real_fidelity_no_delete BEFORE DELETE ON real_fidelity_runs "
        "BEGIN SELECT RAISE(ABORT, 'real_fidelity_authorization_immutable'); END"
    )


def _replace_fidelity_guards(*, expanded: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_candidate_fidelity_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_fidelity_bounded")
    allowed = (
        "'subject_incomplete','component_count_changed','primary_color_changed',"
        "'core_hardware_changed','product_form_changed','provider_check_unavailable',"
        "'minor_color_drift','minor_detail_uncertain','background_style_drift'"
    )
    if expanded:
        allowed += (
            ",'product_category_changed','component_shape_changed',"
            "'component_arrangement_changed','core_accessory_changed'"
        )
    yellow = "'minor_color_drift','minor_detail_uncertain','background_style_drift'"
    op.execute(
        "CREATE TRIGGER trg_style_suite_candidate_fidelity_insert BEFORE INSERT ON style_suite_candidate_fidelities "
        "WHEN NOT EXISTS (SELECT 1 FROM style_suite_run_items i JOIN image_candidates c "
        "ON c.id=NEW.image_candidate_id AND c.customer_id=NEW.customer_id AND c.output_job_id=NEW.output_job_id "
        "WHERE i.id=NEW.run_item_id AND i.customer_id=NEW.customer_id AND i.output_job_id=NEW.output_job_id) "
        f"OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN ({allowed})) "
        "OR (NEW.fidelity_state='green' AND json_array_length(NEW.fidelity_flags) != 0) "
        "OR (NEW.fidelity_state='yellow' AND (json_array_length(NEW.fidelity_flags)=0 "
        f"OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN ({yellow})))) "
        "OR (NEW.fidelity_state='red' AND json_array_length(NEW.fidelity_flags) = 0) "
        "OR (NEW.fidelity_state='unknown' AND (json_array_length(NEW.fidelity_flags) != 1 "
        "OR json_extract(NEW.fidelity_flags, '$[0]') != 'provider_check_unavailable')) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_candidate_fidelity_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_fidelity_bounded BEFORE UPDATE OF fidelity_state, fidelity_flags ON style_suite_run_items "
        f"WHEN EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN ({allowed})) "
        "OR (NEW.fidelity_state='green' AND json_array_length(NEW.fidelity_flags) != 0) "
        "OR (NEW.fidelity_state='yellow' AND (json_array_length(NEW.fidelity_flags)=0 "
        f"OR EXISTS (SELECT 1 FROM json_each(NEW.fidelity_flags) WHERE value NOT IN ({yellow})))) "
        "OR (NEW.fidelity_state='red' AND json_array_length(NEW.fidelity_flags) = 0) "
        "OR (NEW.fidelity_state='unknown' AND (json_array_length(NEW.fidelity_flags) != 1 "
        "OR json_extract(NEW.fidelity_flags, '$[0]') != 'provider_check_unavailable')) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_fidelity_invalid'); END"
    )
