"""Persist the one-run paid image pilot authorization before dispatch."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_real_pilot_authorization_ledger"
down_revision: str | None = "0022_style_suite_pilot_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "real_image_pilot_authorizations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("output_job_id", sa.String(36), nullable=False),
        sa.Column("budget_scope", sa.String(64), nullable=False),
        sa.Column("authorized_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("dispatch_state", sa.String(32), nullable=False),
        sa.Column("outbound_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_real_pilot_authorizations_customer_job",
        ),
        sa.UniqueConstraint("output_job_id", name="uq_real_pilot_authorization_job"),
        sa.UniqueConstraint("budget_scope", name="uq_real_pilot_authorization_scope"),
        sa.CheckConstraint("authorized_usd > 0", name="ck_real_pilot_authorized_positive"),
        sa.CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_real_pilot_dispatch_state",
        ),
        sa.CheckConstraint("outbound_attempts >= 0", name="ck_real_pilot_attempts_nonnegative"),
    )
    op.create_index(
        "ix_real_pilot_authorizations_customer",
        "real_image_pilot_authorizations",
        ["customer_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_real_pilot_authorization_immutable "
        "BEFORE UPDATE OF customer_id, output_job_id, budget_scope, authorized_usd "
        "ON real_image_pilot_authorizations "
        "WHEN NEW.customer_id IS NOT OLD.customer_id "
        "OR NEW.output_job_id IS NOT OLD.output_job_id "
        "OR NEW.budget_scope IS NOT OLD.budget_scope "
        "OR NEW.authorized_usd IS NOT OLD.authorized_usd "
        "BEGIN SELECT RAISE(ABORT, 'real_pilot_authorization_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_real_pilot_authorization_no_delete "
        "BEFORE DELETE ON real_image_pilot_authorizations "
        "BEGIN SELECT RAISE(ABORT, 'real_pilot_authorization_immutable'); END"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM real_image_pilot_authorizations"))
        .scalar_one()
    ):
        raise RuntimeError("cannot discard paid pilot authorization evidence")
    op.execute("DROP TRIGGER IF EXISTS trg_real_pilot_authorization_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_real_pilot_authorization_immutable")
    op.drop_index("ix_real_pilot_authorizations_customer", table_name="real_image_pilot_authorizations")
    op.drop_table("real_image_pilot_authorizations")
