"""add durable storage cleanup jobs

Revision ID: 0004_storage_cleanup_jobs
Revises: 0003_upload_claims
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_storage_cleanup_jobs"
down_revision = "0003_upload_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_cleanup_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("blob_id", postgresql.UUID(as_uuid=True)),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("provider_upload_id", sa.String(512)),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_storage_cleanup_jobs_blob_id", "storage_cleanup_jobs", ["blob_id"]
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM storage_cleanup_jobs LIMIT 1"))
        .scalar()
        is not None
    ):
        raise RuntimeError(
            "cannot downgrade 0004_storage_cleanup_jobs: pending cleanup jobs exist; "
            "process or remove them before downgrading"
        )
    op.drop_index("ix_storage_cleanup_jobs_blob_id", table_name="storage_cleanup_jobs")
    op.drop_table("storage_cleanup_jobs")
