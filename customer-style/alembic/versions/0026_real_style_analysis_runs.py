"""Add the single-dispatch paid style-analysis ledger."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_real_style_analysis_runs"
down_revision: str | None = "0025_asset_store_recovery_and_style_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "real_style_analysis_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("authorized_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("reference_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("reference_selection_version", sa.String(100), nullable=False),
        sa.Column("library_sample_ids", sa.JSON(), nullable=False),
        sa.Column("selected_sample_ids", sa.JSON(), nullable=False),
        sa.Column("dispatch_state", sa.String(32), nullable=False),
        sa.Column("outbound_attempts", sa.Integer(), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "idempotency_key", name="uq_real_style_analysis_customer_key"),
        sa.CheckConstraint("authorized_usd > 0", name="ck_real_style_analysis_usd_positive"),
        sa.CheckConstraint("dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')", name="ck_real_style_analysis_dispatch_state"),
        sa.CheckConstraint("outbound_attempts IN (0, 1)", name="ck_real_style_analysis_one_attempt"),
    )
    op.create_index("ix_real_style_analysis_customer", "real_style_analysis_runs", ["customer_id"])
    op.execute("CREATE TRIGGER trg_real_style_analysis_scope_immutable BEFORE UPDATE ON real_style_analysis_runs WHEN OLD.customer_id != NEW.customer_id OR OLD.idempotency_key != NEW.idempotency_key OR OLD.model != NEW.model OR OLD.authorized_usd != NEW.authorized_usd OR OLD.reference_manifest_sha256 != NEW.reference_manifest_sha256 OR OLD.reference_selection_version != NEW.reference_selection_version OR OLD.library_sample_ids != NEW.library_sample_ids OR OLD.selected_sample_ids != NEW.selected_sample_ids BEGIN SELECT RAISE(ABORT, 'real_style_analysis_authorization_immutable'); END")
    op.execute("CREATE TRIGGER trg_real_style_analysis_no_delete BEFORE DELETE ON real_style_analysis_runs BEGIN SELECT RAISE(ABORT, 'real_style_analysis_authorization_immutable'); END")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM real_style_analysis_runs")).scalar_one():
        raise RuntimeError("cannot discard paid style analysis evidence")
    op.execute("DROP TRIGGER IF EXISTS trg_real_style_analysis_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_real_style_analysis_scope_immutable")
    op.drop_index("ix_real_style_analysis_customer", table_name="real_style_analysis_runs")
    op.drop_table("real_style_analysis_runs")
