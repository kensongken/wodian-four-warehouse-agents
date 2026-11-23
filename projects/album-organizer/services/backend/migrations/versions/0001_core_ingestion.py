"""core ingestion schema

Revision ID: 0001_core_ingestion
Revises:
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_core_ingestion"
down_revision = None
branch_labels = None
depends_on = None


task_status = postgresql.ENUM(
    "waiting_upload",
    "uploading",
    "ai_processing",
    "waiting_review",
    "completed",
    "paused",
    "partial_failure",
    "cancelled",
    name="task_status",
    create_type=False,
)
asset_status = postgresql.ENUM(
    "pending",
    "uploading",
    "uploaded",
    "failed",
    "skipped_unsupported",
    name="asset_status",
    create_type=False,
)


def audit_columns() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    bind = op.get_bind()
    task_status.create(bind, checkfirst=False)
    asset_status.create(bind, checkfirst=False)

    op.create_table(
        "users",
        *audit_columns(),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("phone_e164", name="uq_users_phone_e164"),
    )
    op.create_table(
        "invites",
        *audit_columns(),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["redeemed_by_user_id"],
            ["users.id"],
            name="fk_invites_redeemed_by_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invites"),
        sa.UniqueConstraint("code", name="uq_invites_code"),
    )
    op.create_table(
        "otp_challenges",
        *audit_columns(),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("invite_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["invite_id"], ["invites.id"], name="fk_otp_challenges_invite_id_invites"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_otp_challenges"),
    )
    op.create_table(
        "refresh_tokens",
        *audit_columns(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_id", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name="fk_refresh_tokens_replaced_by_id_refresh_tokens",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_table(
        "import_tasks",
        *audit_columns(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "status", task_status, server_default="waiting_upload", nullable=False
        ),
        sa.Column("total_assets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("uploaded_assets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_assets", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "unsupported_assets", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.CheckConstraint("total_assets >= 0", name="total_assets_non_negative"),
        sa.CheckConstraint("uploaded_assets >= 0", name="uploaded_assets_non_negative"),
        sa.CheckConstraint("failed_assets >= 0", name="failed_assets_non_negative"),
        sa.CheckConstraint(
            "unsupported_assets >= 0",
            name="unsupported_assets_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_import_tasks_user_id_users",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_tasks"),
        sa.UniqueConstraint("user_id", "id", name="import_tasks_user_id_id_key"),
    )
    op.create_index("ix_import_tasks_user_id", "import_tasks", ["user_id"])
    op.create_table(
        "stored_blobs",
        *audit_columns(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("provider_upload_id", sa.String(length=512), nullable=True),
        sa.Column("provider_etag", sa.String(length=512), nullable=True),
        sa.Column("provider_version_id", sa.String(length=512), nullable=True),
        sa.Column("completed_parts", sa.Text(), nullable=True),
        sa.Column(
            "is_ready", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_lowercase_hex"),
        sa.CheckConstraint("byte_size > 0", name="byte_size_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_stored_blobs_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_stored_blobs"),
        sa.UniqueConstraint("user_id", "id", name="stored_blobs_user_id_id_key"),
        sa.UniqueConstraint(
            "user_id", "sha256", "byte_size", name="stored_blobs_user_sha256_size_key"
        ),
    )
    op.create_index("ix_stored_blobs_user_id", "stored_blobs", ["user_id"])
    op.create_table(
        "assets",
        *audit_columns(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("blob_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("local_asset_id", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", asset_status, server_default="pending", nullable=False),
        sa.Column("unsupported_reason", sa.String(length=128), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = 'skipped_unsupported' "
            "AND (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$') "
            "AND (byte_size IS NULL OR byte_size >= 0)) "
            "OR (status <> 'skipped_unsupported' AND sha256 IS NOT NULL "
            "AND byte_size IS NOT NULL AND sha256 ~ '^[0-9a-f]{64}$' AND byte_size > 0)",
            name="accepted_content_metadata",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "blob_id"],
            ["stored_blobs.user_id", "stored_blobs.id"],
            name="fk_assets_user_id_blob_id_stored_blobs",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "task_id"],
            ["import_tasks.user_id", "import_tasks.id"],
            name="fk_assets_user_id_task_id_import_tasks",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_assets_user_id_users",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assets"),
        sa.UniqueConstraint(
            "user_id",
            "task_id",
            "local_asset_id",
            name="assets_user_task_local_asset_key",
        ),
    )
    op.create_index("ix_assets_blob_id", "assets", ["blob_id"])
    op.create_index("ix_assets_task_id", "assets", ["task_id"])
    op.create_index("ix_assets_user_id", "assets", ["user_id"])
    op.create_table(
        "outbox_events",
        *audit_columns(),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.UniqueConstraint(
            "topic",
            "aggregate_id",
            "sequence",
            name="outbox_events_topic_aggregate_sequence_key",
        ),
    )
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_events_aggregate_id", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_assets_user_id", table_name="assets")
    op.drop_index("ix_assets_task_id", table_name="assets")
    op.drop_index("ix_assets_blob_id", table_name="assets")
    op.drop_table("assets")
    op.drop_index("ix_stored_blobs_user_id", table_name="stored_blobs")
    op.drop_table("stored_blobs")
    op.drop_index("ix_import_tasks_user_id", table_name="import_tasks")
    op.drop_table("import_tasks")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("otp_challenges")
    op.drop_table("invites")
    op.drop_table("users")
    op.execute("DROP TYPE asset_status")
    op.execute("DROP TYPE task_status")
