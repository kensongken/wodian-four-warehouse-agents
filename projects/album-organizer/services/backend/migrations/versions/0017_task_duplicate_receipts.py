"""add task-local content duplicate receipts

Revision ID: 0017_task_duplicate_receipts
Revises: 0016_upload_binding_authority
Create Date: 2026-08-30
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_task_duplicate_receipts"
down_revision = "0016_upload_binding_authority"
branch_labels = None
depends_on = None

OLD_ASSET_STATUSES = (
    "pending",
    "uploading",
    "uploaded",
    "failed",
    "skipped_unsupported",
)
NEW_ASSET_STATUSES = (*OLD_ASSET_STATUSES, "skipped_duplicate")
ACCEPTED_CONTENT_METADATA_CHECK = (
    "(status = 'skipped_unsupported' "
    "AND (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$') "
    "AND (byte_size IS NULL OR byte_size >= 0)) "
    "OR (status <> 'skipped_unsupported' AND sha256 IS NOT NULL "
    "AND byte_size IS NOT NULL AND sha256 ~ '^[0-9a-f]{64}$' AND byte_size > 0)"
)


def _replace_asset_status(values: tuple[str, ...], old_type_name: str) -> None:
    labels = ", ".join(f"'{value}'" for value in values)
    op.execute("ALTER TABLE assets ALTER COLUMN status DROP DEFAULT")
    op.execute(f"ALTER TYPE asset_status RENAME TO {old_type_name}")
    op.execute(f"CREATE TYPE asset_status AS ENUM ({labels})")
    op.execute(
        "ALTER TABLE assets ALTER COLUMN status TYPE asset_status "
        "USING status::text::asset_status"
    )
    op.execute(
        "ALTER TABLE assets ALTER COLUMN status SET DEFAULT 'pending'::asset_status"
    )
    op.execute(f"DROP TYPE {old_type_name}")


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_assets_accepted_content_metadata"), "assets", type_="check"
    )
    _replace_asset_status(NEW_ASSET_STATUSES, "asset_status_0016")
    op.create_check_constraint(
        op.f("ck_assets_accepted_content_metadata"),
        "assets",
        ACCEPTED_CONTENT_METADATA_CHECK,
    )
    op.add_column(
        "assets",
        sa.Column(
            "canonical_asset_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "assets_user_task_id_key", "assets", ["user_id", "task_id", "id"]
    )
    op.create_foreign_key(
        "fk_assets_user_task_canonical_asset_assets",
        "assets",
        "assets",
        ["user_id", "task_id", "canonical_asset_id"],
        ["user_id", "task_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        op.f("ck_assets_duplicate_receipt_linkage"),
        "assets",
        "(status = 'skipped_duplicate' AND canonical_asset_id IS NOT NULL "
        "AND canonical_asset_id <> id AND blob_id IS NULL) OR "
        "(status <> 'skipped_duplicate' AND canonical_asset_id IS NULL)",
    )
    op.create_index("ix_assets_canonical_asset_id", "assets", ["canonical_asset_id"])
    op.create_index(
        "ix_assets_task_content_canonical",
        "assets",
        ["user_id", "task_id", "sha256", "byte_size"],
        postgresql_where=sa.text(
            "canonical_asset_id IS NULL AND status IN "
            "('pending', 'uploading', 'uploaded', 'failed')"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE assets IN ACCESS EXCLUSIVE MODE"))
    has_receipts = connection.execute(
        sa.text(
            "SELECT 1 FROM assets WHERE status = 'skipped_duplicate' "
            "OR canonical_asset_id IS NOT NULL LIMIT 1"
        )
    ).scalar()
    if has_receipts is not None:
        raise RuntimeError(
            "cannot downgrade 0017_task_duplicate_receipts: duplicate "
            "receipt provenance exists; export or explicitly delete it first"
        )

    op.drop_index("ix_assets_task_content_canonical", table_name="assets")
    op.drop_index("ix_assets_canonical_asset_id", table_name="assets")
    op.drop_constraint(
        op.f("ck_assets_duplicate_receipt_linkage"), "assets", type_="check"
    )
    op.drop_constraint(
        "fk_assets_user_task_canonical_asset_assets", "assets", type_="foreignkey"
    )
    op.drop_constraint("assets_user_task_id_key", "assets", type_="unique")
    op.drop_column("assets", "canonical_asset_id")
    op.drop_constraint(
        op.f("ck_assets_accepted_content_metadata"), "assets", type_="check"
    )
    _replace_asset_status(OLD_ASSET_STATUSES, "asset_status_0017")
    op.create_check_constraint(
        op.f("ck_assets_accepted_content_metadata"),
        "assets",
        ACCEPTED_CONTENT_METADATA_CHECK,
    )
