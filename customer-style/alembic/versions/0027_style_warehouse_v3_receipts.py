"""Add independent style warehouse V3 receipts and sanitized attempts."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_style_warehouse_v3_receipts"
down_revision: str | None = "0026_identity_locked_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "style_warehouse_v3_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("handoff_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("checked_payload_sha256", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("rejection_code", sa.String(64), nullable=True),
        sa.Column("generation_job_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_style_warehouse_v3_tenant_key"
        ),
        sa.UniqueConstraint(
            "tenant_id", "handoff_id", name="uq_style_warehouse_v3_tenant_handoff"
        ),
        sa.CheckConstraint(
            "decision IN ('accepted', 'rejected')",
            name="ck_style_warehouse_v3_decision",
        ),
        sa.CheckConstraint(
            "generation_job_id IS NULL",
            name="ck_style_warehouse_v3_generation_empty",
        ),
        sa.CheckConstraint(
            "(decision = 'accepted' AND rejection_code IS NULL) OR "
            "(decision = 'rejected' AND rejection_code IS NOT NULL)",
            name="ck_style_warehouse_v3_rejection_code",
        ),
    )
    op.create_index(
        "ix_style_warehouse_v3_tenant_decided",
        "style_warehouse_v3_receipts",
        ["tenant_id", "decided_at"],
    )
    op.create_table(
        "style_warehouse_v3_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=True),
        sa.Column("handoff_id", sa.String(36), nullable=True),
        sa.Column("payload_sha256", sa.String(64), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "result IN ('accepted', 'rejected', 'duplicate', 'conflict', 'error')",
            name="ck_style_warehouse_v3_attempt_result",
        ),
    )
    op.create_index(
        "ix_style_warehouse_v3_attempt_received",
        "style_warehouse_v3_attempts",
        ["tenant_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_style_warehouse_v3_attempt_received",
        table_name="style_warehouse_v3_attempts",
    )
    op.drop_table("style_warehouse_v3_attempts")
    op.drop_index(
        "ix_style_warehouse_v3_tenant_decided",
        table_name="style_warehouse_v3_receipts",
    )
    op.drop_table("style_warehouse_v3_receipts")
