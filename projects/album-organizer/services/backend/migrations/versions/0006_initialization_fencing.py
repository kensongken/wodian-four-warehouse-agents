"""add initialization claim fencing generation

Revision ID: 0006_initialization_fencing
Revises: 0005_completion_fencing
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_initialization_fencing"
down_revision = "0005_completion_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stored_blobs",
        sa.Column(
            "initialization_generation",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("stored_blobs", "initialization_generation")
