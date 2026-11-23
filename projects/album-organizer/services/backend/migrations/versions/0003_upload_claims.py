"""harden multipart upload claims and ready deduplication

Revision ID: 0003_upload_claims
Revises: 0002_otp_delivery_state
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_upload_claims"
down_revision = "0002_otp_delivery_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "stored_blobs_user_sha256_size_key", "stored_blobs", type_="unique"
    )
    op.create_index(
        "stored_blobs_user_ready_sha256_size_key",
        "stored_blobs",
        ["user_id", "sha256", "byte_size"],
        unique=True,
        postgresql_where=sa.text("is_ready"),
    )
    op.add_column("stored_blobs", sa.Column("initialization_claim_id", sa.String(64)))
    op.add_column(
        "stored_blobs",
        sa.Column("initialization_claimed_at", sa.DateTime(timezone=True)),
    )
    op.add_column("stored_blobs", sa.Column("completion_claim_id", sa.String(64)))
    op.add_column(
        "stored_blobs", sa.Column("completion_claimed_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "stored_blobs",
        sa.Column(
            "cleanup_pending",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    # The predecessor's unconditional digest uniqueness cannot represent two
    # independently in-flight uploads.  Refuse before *any* DDL instead of
    # discarding valid partial-index-era rows or failing halfway through a
    # downgrade.  Alembic's transactional DDL keeps the revision unchanged.
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT 1
            FROM stored_blobs
            GROUP BY user_id, sha256, byte_size
            HAVING count(*) > 1
            LIMIT 1
            """
            )
        )
        .scalar()
    )
    if duplicates is not None:
        raise RuntimeError(
            "cannot downgrade 0003_upload_claims: duplicate stored_blobs "
            "exist under the ready-only digest uniqueness rule; remove or "
            "resolve in-flight duplicates before downgrading"
        )
    op.drop_column("stored_blobs", "cleanup_pending")
    op.drop_column("stored_blobs", "completion_claimed_at")
    op.drop_column("stored_blobs", "completion_claim_id")
    op.drop_column("stored_blobs", "initialization_claimed_at")
    op.drop_column("stored_blobs", "initialization_claim_id")
    op.drop_index("stored_blobs_user_ready_sha256_size_key", table_name="stored_blobs")
    op.create_unique_constraint(
        "stored_blobs_user_sha256_size_key",
        "stored_blobs",
        ["user_id", "sha256", "byte_size"],
    )
