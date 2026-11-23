"""Persist immutable human foreground mask revision lineage."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_product_foreground_mask_revisions"
down_revision: str | None = "0035_pixel_preserving_isolated_style"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_foreground_mask_revisions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("base_foreground_version_id", sa.String(36), nullable=False),
        sa.Column("result_foreground_version_id", sa.String(36), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("base_mask_sha256", sa.String(64), nullable=False),
        sa.Column("result_mask_sha256", sa.String(64), nullable=False),
        sa.Column("edit_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("editor_version", sa.String(100), nullable=False),
        sa.Column("magic_wand_count", sa.Integer(), nullable=False),
        sa.Column("erase_stroke_count", sa.Integer(), nullable=False),
        sa.Column("restore_stroke_count", sa.Integer(), nullable=False),
        sa.Column("added_pixel_count", sa.Integer(), nullable=False),
        sa.Column("deleted_pixel_count", sa.Integer(), nullable=False),
        sa.Column("result_pixel_count", sa.Integer(), nullable=False),
        sa.Column("component_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "base_foreground_version_id"],
            [
                "product_foreground_versions.customer_id",
                "product_foreground_versions.id",
            ],
            name="fk_mask_revision_customer_base",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "result_foreground_version_id"],
            [
                "product_foreground_versions.customer_id",
                "product_foreground_versions.id",
            ],
            name="fk_mask_revision_customer_result",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_mask_revisions_customer_id"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_mask_revisions_customer_key",
        ),
        sa.UniqueConstraint(
            "result_foreground_version_id", name="uq_mask_revisions_result"
        ),
        sa.CheckConstraint(
            "base_foreground_version_id != result_foreground_version_id",
            name="ck_mask_revisions_distinct_versions",
        ),
        sa.CheckConstraint(
            "length(source_content_sha256) = 64 "
            "AND length(base_mask_sha256) = 64 "
            "AND length(result_mask_sha256) = 64 "
            "AND length(edit_manifest_sha256) = 64",
            name="ck_mask_revisions_digests",
        ),
        sa.CheckConstraint(
            "editor_version = 'human-mask-editor-v1'",
            name="ck_mask_revisions_editor",
        ),
        sa.CheckConstraint(
            "magic_wand_count >= 0 AND erase_stroke_count >= 0 "
            "AND restore_stroke_count >= 0",
            name="ck_mask_revisions_operation_counts",
        ),
        sa.CheckConstraint(
            "added_pixel_count >= 0 AND deleted_pixel_count >= 0 "
            "AND result_pixel_count > 0 AND component_count > 0",
            name="ck_mask_revisions_metrics",
        ),
    )
    op.create_index(
        "ix_mask_revisions_customer_asset",
        "product_foreground_mask_revisions",
        ["customer_id", "product_asset_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_mask_revision_scope "
        "BEFORE INSERT ON product_foreground_mask_revisions WHEN "
        "NEW.base_foreground_version_id != NEW.result_foreground_version_id "
        "AND NOT EXISTS ("
        "SELECT 1 FROM product_foreground_versions base "
        "JOIN product_foreground_versions result "
        "ON result.id = NEW.result_foreground_version_id "
        "AND result.customer_id = NEW.customer_id "
        "WHERE base.id = NEW.base_foreground_version_id "
        "AND base.customer_id = NEW.customer_id "
        "AND base.product_asset_id = NEW.product_asset_id "
        "AND result.product_asset_id = NEW.product_asset_id "
        "AND base.source_content_sha256 = NEW.source_content_sha256 "
        "AND result.source_content_sha256 = NEW.source_content_sha256 "
        "AND base.mask_sha256 = NEW.base_mask_sha256 "
        "AND result.mask_sha256 = NEW.result_mask_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'mask_revision_scope_mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mask_revision_immutable "
        "BEFORE UPDATE ON product_foreground_mask_revisions "
        "BEGIN SELECT RAISE(ABORT, 'mask_revision_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_mask_revision_no_delete "
        "BEFORE DELETE ON product_foreground_mask_revisions "
        "BEGIN SELECT RAISE(ABORT, 'mask_revision_immutable'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT count(*) FROM product_foreground_mask_revisions")
    ).scalar_one():
        raise RuntimeError("cannot discard foreground mask revision evidence")
    op.execute("DROP TRIGGER IF EXISTS trg_mask_revision_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_mask_revision_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_mask_revision_scope")
    op.drop_index(
        "ix_mask_revisions_customer_asset",
        table_name="product_foreground_mask_revisions",
    )
    op.drop_table("product_foreground_mask_revisions")
