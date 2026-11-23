"""Add the durable local classification handoff idempotency fence."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_classification_bridge_claims"
down_revision: str | None = "0010_authenticity_completion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "classification_bridge_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("handoff_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("handoff_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "handoff_id", name="uq_bridge_claim_customer_handoff"),
        sa.UniqueConstraint("customer_id", "idempotency_key", name="uq_bridge_claim_customer_key"),
    )
    op.create_index(
        "ix_classification_bridge_claims_customer_id",
        "classification_bridge_claims",
        ["customer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_bridge_claims_customer_id",
        table_name="classification_bridge_claims",
    )
    op.drop_table("classification_bridge_claims")
