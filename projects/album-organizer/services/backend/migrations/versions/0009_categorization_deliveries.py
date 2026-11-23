"""add durable categorization stream deliveries

Revision ID: 0009_categorization_deliveries
Revises: 0008_cleanup_state_invariants
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_categorization_deliveries"
down_revision = "0008_cleanup_state_invariants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "categorization_deliveries",
        sa.Column("consumer_name", sa.String(length=128), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("source_stream_id", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retryable_failed', 'completed', 'terminal_failed')",
            name=op.f("ck_categorization_deliveries_status_allowed"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND claim_generation >= 0",
            name=op.f("ck_categorization_deliveries_counters_nonnegative"),
        ),
        sa.CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL AND claim_expires_at IS NULL)",
            name=op.f("ck_categorization_deliveries_claim_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categorization_deliveries")),
        sa.UniqueConstraint(
            "consumer_name",
            "source_event_id",
            name="categorization_deliveries_consumer_event_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("categorization_deliveries")
