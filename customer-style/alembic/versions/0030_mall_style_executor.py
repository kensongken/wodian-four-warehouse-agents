"""Durable Mall execution lease, available in the local SQLite lineage."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_mall_style_executor"
down_revision: str | None = "0029_mall_style_results_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mall_style_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "queue_id",
            sa.String(36),
            sa.ForeignKey("mall_style_generation_queue_items.id"),
            nullable=False,
        ),
        sa.Column("claim_token", sa.String(36), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("outbound_attempts", sa.Integer(), nullable=False),
        sa.Column("planned_job_id", sa.String(36), nullable=False),
        sa.Column("failure_reason", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("queue_id", name="uq_mall_style_execution_queue"),
        sa.CheckConstraint("outbound_attempts IN (0,1)", name="ck_mall_execution_once"),
        sa.CheckConstraint(
            "state IN ('leased','reserved','completed','blocked','uncertain')",
            name="ck_mall_execution_state",
        ),
    )
    op.execute("""CREATE TRIGGER trg_mall_execution_dispatch_fence BEFORE UPDATE ON mall_style_executions
    WHEN NEW.queue_id IS NOT OLD.queue_id OR NEW.planned_job_id IS NOT OLD.planned_job_id
    OR NEW.outbound_attempts < OLD.outbound_attempts
    OR (OLD.outbound_attempts = 1 AND (NEW.state = 'leased' OR NEW.claim_token IS NOT OLD.claim_token))
    BEGIN SELECT RAISE(ABORT, 'mall_execution_dispatch_fence_immutable'); END""")
    op.execute("""CREATE TRIGGER trg_mall_execution_no_delete BEFORE DELETE ON mall_style_executions
    BEGIN SELECT RAISE(ABORT, 'mall_execution_dispatch_fence_immutable'); END""")


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_mall_execution_no_delete")
    op.execute("DROP TRIGGER trg_mall_execution_dispatch_fence")
    op.drop_table("mall_style_executions")
