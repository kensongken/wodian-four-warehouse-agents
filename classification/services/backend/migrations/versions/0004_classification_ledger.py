"""Create the immutable product classification ledger.

Revision ID: 0004_classification_ledger
Revises: 0003_sales_strategy_inputs
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_classification_ledger"
down_revision: str | None = "0003_sales_strategy_inputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "product_classification_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_material_package_id", UUID, nullable=False),
        sa.Column("base_material_package_version", sa.Integer(), nullable=False),
        sa.Column("material_label", sa.String(128), nullable=False),
        sa.Column("product_form", sa.String(64), nullable=False),
        sa.Column("main_asset_id", UUID, nullable=False),
        sa.Column("visual_observations", JSONB, nullable=False),
        sa.Column("selling_point_candidates", JSONB, nullable=False),
        sa.Column("inventory_status", sa.String(24), nullable=True),
        sa.Column("price_status", sa.String(24), nullable=False),
        sa.Column("price_candidate", JSONB, nullable=True),
        sa.Column("service_rule_summaries", JSONB, nullable=False),
        sa.Column("source_image_set_sha256", sa.String(64), nullable=False),
        sa.Column("review_status", sa.String(24), nullable=False),
        sa.Column("material_evidence", sa.String(32), nullable=False),
        sa.Column("review_source", sa.String(64), nullable=False),
        sa.Column("reviewer_ref", sa.String(128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", UUID, nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_status = 'human_confirmed'",
            name="ck_classification_versions_review_status",
        ),
        sa.CheckConstraint(
            "price_status IN ('pending', 'human_confirmed')",
            name="ck_classification_versions_price_status",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_classification_versions_product"
        ),
        sa.ForeignKeyConstraint(
            ["base_material_package_id"],
            ["material_packages.id"],
            name="fk_classification_versions_material_package",
        ),
        sa.ForeignKeyConstraint(
            ["main_asset_id"],
            ["source_images.id"],
            name="fk_classification_versions_main_asset",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_product_classification_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_classification_versions_tenant_product_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            name="uq_classification_versions_tenant_request",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_classification_versions_tenant_product_content",
        ),
    )
    op.create_index(
        "ix_classification_versions_tenant_product_version",
        "product_classification_versions",
        ["tenant_id", "product_id", "version"],
    )

    op.create_table(
        "product_classification_assets",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("classification_version_id", UUID, nullable=False),
        sa.Column("source_image_id", UUID, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["classification_version_id"],
            ["product_classification_versions.id"],
            name="fk_classification_assets_version",
        ),
        sa.ForeignKeyConstraint(
            ["source_image_id"],
            ["source_images.id"],
            name="fk_classification_assets_source_image",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_product_classification_assets"),
        sa.UniqueConstraint(
            "classification_version_id",
            "source_image_id",
            name="uq_classification_assets_version_source",
        ),
        sa.UniqueConstraint(
            "classification_version_id",
            "position",
            name="uq_classification_assets_version_position",
        ),
    )
    op.create_index(
        "ix_classification_assets_tenant_version_position",
        "product_classification_assets",
        ["tenant_id", "classification_version_id", "position"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_assets_tenant_version_position",
        table_name="product_classification_assets",
    )
    op.drop_table("product_classification_assets")
    op.drop_index(
        "ix_classification_versions_tenant_product_version",
        table_name="product_classification_versions",
    )
    op.drop_table("product_classification_versions")
