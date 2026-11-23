"""add OTP delivery state

Revision ID: 0002_otp_delivery_state
Revises: 0001_core_ingestion
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_otp_delivery_state"
down_revision = "0001_core_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "otp_challenges",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("otp_challenges", "delivered_at")
