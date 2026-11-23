"""fence cleanup work and enforce tenant-safe cleanup references

Revision ID: 0007_cleanup_job_claims
Revises: 0006_initialization_fencing
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_cleanup_job_claims"
down_revision = "0006_initialization_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storage_cleanup_jobs",
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
    )
    op.add_column("storage_cleanup_jobs", sa.Column("claim_id", sa.String(64)))
    op.add_column(
        "storage_cleanup_jobs", sa.Column("claimed_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "storage_cleanup_jobs",
        sa.Column("claim_generation", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("storage_cleanup_jobs", sa.Column("terminal_code", sa.String(64)))
    op.create_check_constraint(
        "storage_cleanup_jobs_operation_allowed",
        "storage_cleanup_jobs",
        "operation IN ('abort_multipart', 'delete_object')",
    )
    op.create_check_constraint(
        "storage_cleanup_jobs_provider_id_by_operation",
        "storage_cleanup_jobs",
        "(operation = 'abort_multipart' AND provider_upload_id IS NOT NULL) OR (operation = 'delete_object' AND provider_upload_id IS NULL)",
    )
    op.create_check_constraint(
        "storage_cleanup_jobs_status_allowed",
        "storage_cleanup_jobs",
        "status IN ('pending', 'processing', 'dead')",
    )
    op.create_foreign_key(
        "fk_cleanup_jobs_user_blob",
        "storage_cleanup_jobs",
        "stored_blobs",
        ["user_id", "blob_id"],
        ["user_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM storage_cleanup_jobs LIMIT 1"))
        .scalar()
        is not None
    ):
        raise RuntimeError(
            "cannot downgrade 0007_cleanup_job_claims: cleanup jobs exist; process them before downgrading"
        )
    op.drop_constraint(
        "fk_cleanup_jobs_user_blob", "storage_cleanup_jobs", type_="foreignkey"
    )
    op.drop_constraint(
        "storage_cleanup_jobs_status_allowed", "storage_cleanup_jobs", type_="check"
    )
    op.drop_constraint(
        "storage_cleanup_jobs_provider_id_by_operation",
        "storage_cleanup_jobs",
        type_="check",
    )
    op.drop_constraint(
        "storage_cleanup_jobs_operation_allowed", "storage_cleanup_jobs", type_="check"
    )
    op.drop_column("storage_cleanup_jobs", "terminal_code")
    op.drop_column("storage_cleanup_jobs", "claim_generation")
    op.drop_column("storage_cleanup_jobs", "claimed_at")
    op.drop_column("storage_cleanup_jobs", "claim_id")
    op.drop_column("storage_cleanup_jobs", "status")
