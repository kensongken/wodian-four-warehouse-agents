"""Persist immutable customer-owned normalized product subjects."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_product_subject_seals"
down_revision: str | None = "0024_scene_style_origin_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_subject_seals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("transform_permission_event_id", sa.String(36), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("segmentation_adapter", sa.String(100), nullable=False),
        sa.Column("segmentation_adapter_version", sa.String(100), nullable=False),
        sa.Column("mask_sha256", sa.String(64), nullable=False),
        sa.Column("normalized_subject_sha256", sa.String(64), nullable=False),
        sa.Column("normalized_width", sa.Integer(), nullable=False),
        sa.Column("normalized_height", sa.Integer(), nullable=False),
        sa.Column("core_mask_pixel_count", sa.Integer(), nullable=False),
        sa.Column("feather_width_px", sa.Integer(), nullable=False),
        sa.Column("idempotency_fingerprint", sa.String(64), nullable=False),
        sa.Column("artifact_store_namespace", sa.String(64), nullable=False),
        sa.Column("seal_version", sa.Integer(), nullable=False),
        sa.Column("synthetic_test_only", sa.Boolean(), nullable=False),
        sa.Column("publishable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "product_asset_id"],
            ["product_assets.customer_id", "product_assets.id"],
            name="fk_product_subject_seals_customer_asset",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "transform_permission_event_id"],
            [
                "product_asset_permission_events.customer_id",
                "product_asset_permission_events.id",
            ],
            name="fk_product_subject_seals_customer_permission",
        ),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_product_subject_seals_customer_id_id"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_fingerprint",
            name="uq_product_subject_seals_customer_fingerprint",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_product_subject_seals_source_sha256_length",
        ),
        sa.CheckConstraint(
            "length(mask_sha256) = 64",
            name="ck_product_subject_seals_mask_sha256_length",
        ),
        sa.CheckConstraint(
            "length(normalized_subject_sha256) = 64",
            name="ck_product_subject_seals_subject_sha256_length",
        ),
        sa.CheckConstraint(
            "length(idempotency_fingerprint) = 64",
            name="ck_product_subject_seals_fingerprint_length",
        ),
        sa.CheckConstraint(
            "normalized_width > 0 AND normalized_height > 0",
            name="ck_product_subject_seals_dimensions_positive",
        ),
        sa.CheckConstraint(
            "core_mask_pixel_count > 0",
            name="ck_product_subject_seals_core_pixels_positive",
        ),
        sa.CheckConstraint(
            "feather_width_px >= 0",
            name="ck_product_subject_seals_feather_nonnegative",
        ),
        sa.CheckConstraint("seal_version = 1", name="ck_product_subject_seals_version"),
        sa.CheckConstraint(
            "publishable = 0", name="ck_product_subject_seals_not_publishable"
        ),
    )
    op.create_index(
        "ix_product_subject_seals_customer_id",
        "product_subject_seals",
        ["customer_id"],
    )
    op.create_index(
        "ix_product_subject_seals_product_id",
        "product_subject_seals",
        ["product_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_product_subject_seals_no_update "
        "BEFORE UPDATE ON product_subject_seals "
        "BEGIN SELECT RAISE(ABORT, 'product_subject_seal_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_subject_seals_no_delete "
        "BEFORE DELETE ON product_subject_seals "
        "BEGIN SELECT RAISE(ABORT, 'product_subject_seal_immutable'); END"
    )


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM product_subject_seals"))
        .scalar_one()
    )
    if count:
        raise RuntimeError("cannot discard product subject seal evidence")
    op.execute("DROP TRIGGER IF EXISTS trg_product_subject_seals_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_product_subject_seals_no_delete")
    op.drop_index(
        "ix_product_subject_seals_product_id", table_name="product_subject_seals"
    )
    op.drop_index(
        "ix_product_subject_seals_customer_id", table_name="product_subject_seals"
    )
    op.drop_table("product_subject_seals")
