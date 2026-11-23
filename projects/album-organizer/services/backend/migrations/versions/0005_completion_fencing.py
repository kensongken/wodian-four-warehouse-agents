"""add completion claim fencing generation

Revision ID: 0005_completion_fencing
Revises: 0004_storage_cleanup_jobs
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_completion_fencing"
down_revision = "0004_storage_cleanup_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stored_blobs",
        sa.Column(
            "completion_generation", sa.Integer(), server_default="0", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("stored_blobs", "completion_generation")
