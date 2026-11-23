"""Create the Phase 1 integration spine.

Revision ID: 0001_integration_spine
Revises:
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_integration_spine"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "inbox_events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("payload_sha256", sa.CHAR(64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_inbox_events"),
        sa.UniqueConstraint(
            "source_system",
            "event_id",
            name="uq_inbox_events_source_event",
        ),
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("topic", sa.String(96), nullable=False),
        sa.Column("aggregate_key", sa.String(128), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="ck_outbox_events_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
    )
    op.create_index(
        "ix_outbox_events_tenant_status_available",
        "outbox_events",
        ["tenant_id", "status", "available_at"],
    )

    op.create_table(
        "products",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_code", sa.String(64), nullable=False),
        sa.Column("source_task_id", UUID, nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("form", sa.String(64), nullable=False),
        sa.Column("classification_confidence", sa.Numeric(5, 4), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint(
            "tenant_id",
            "product_code",
            name="uq_products_tenant_product_code",
        ),
    )
    op.create_index(
        "ix_products_tenant_product_code",
        "products",
        ["tenant_id", "product_code"],
    )

    op.create_table(
        "source_images",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("upstream_asset_id", UUID, nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("quality_confidence", sa.Numeric(5, 4), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_source_images_product"),
        sa.PrimaryKeyConstraint("id", name="pk_source_images"),
        sa.UniqueConstraint(
            "tenant_id",
            "upstream_asset_id",
            name="uq_source_images_tenant_upstream_asset",
        ),
    )
    op.create_index(
        "ix_source_images_tenant_product_id",
        "source_images",
        ["tenant_id", "product_id"],
    )

    op.create_table(
        "material_packages",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("content_status", sa.String(24), nullable=False),
        sa.Column("price_status", sa.String(24), nullable=False),
        sa.Column("published_event_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status = 'source_ready'", name="ck_material_packages_status"),
        sa.CheckConstraint(
            "content_status = 'pending'",
            name="ck_material_packages_content_status",
        ),
        sa.CheckConstraint(
            "price_status = 'pending'",
            name="ck_material_packages_price_status",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_material_packages_product",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_material_packages"),
        sa.UniqueConstraint(
            "tenant_id",
            "published_event_id",
            name="uq_material_packages_tenant_published_event",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_material_packages_tenant_product_version",
        ),
    )
    op.create_index(
        "ix_material_packages_tenant_product_id",
        "material_packages",
        ["tenant_id", "product_id"],
    )

    op.create_table(
        "material_package_assets",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("package_id", UUID, nullable=False),
        sa.Column("source_image_id", UUID, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_id"],
            ["material_packages.id"],
            name="fk_material_package_assets_package",
        ),
        sa.ForeignKeyConstraint(
            ["source_image_id"],
            ["source_images.id"],
            name="fk_material_package_assets_source_image",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_material_package_assets"),
        sa.UniqueConstraint(
            "tenant_id",
            "package_id",
            "source_image_id",
            name="uq_material_package_assets_source",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "package_id",
            "position",
            name="uq_material_package_assets_position",
        ),
    )
    op.create_index(
        "ix_material_package_assets_tenant_package_id",
        "material_package_assets",
        ["tenant_id", "package_id"],
    )

    op.create_table(
        "photo_templates",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("template_code", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("image_type", sa.String(32), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_photo_templates"),
        sa.UniqueConstraint(
            "tenant_id",
            "template_code",
            "version",
            name="uq_photo_templates_tenant_code_version",
        ),
    )
    op.create_index(
        "ix_photo_templates_tenant_template_code",
        "photo_templates",
        ["tenant_id", "template_code"],
    )

    op.create_table(
        "sales_material_usage",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("package_version", sa.Integer(), nullable=False),
        sa.Column("usage_type", sa.String(24), nullable=False),
        sa.Column("material_ids", JSONB, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_sales_material_usage_product",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sales_material_usage"),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_sales_material_usage_tenant_event",
        ),
    )
    op.create_index(
        "ix_sales_material_usage_tenant_session_id",
        "sales_material_usage",
        ["tenant_id", "session_id"],
    )

    op.create_table(
        "screenshot_imports",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("event_id", UUID, nullable=True),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("session_id", UUID, nullable=True),
        sa.Column("product_code", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_screenshot_imports"),
        sa.UniqueConstraint(
            "tenant_id",
            "sha256",
            name="uq_screenshot_imports_tenant_sha256",
        ),
    )
    op.create_index(
        "ix_screenshot_imports_tenant_created_at",
        "screenshot_imports",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_screenshot_imports_tenant_created_at", table_name="screenshot_imports")
    op.drop_table("screenshot_imports")
    op.drop_index("ix_sales_material_usage_tenant_session_id", table_name="sales_material_usage")
    op.drop_table("sales_material_usage")
    op.drop_index("ix_photo_templates_tenant_template_code", table_name="photo_templates")
    op.drop_table("photo_templates")
    op.drop_index(
        "ix_material_package_assets_tenant_package_id",
        table_name="material_package_assets",
    )
    op.drop_table("material_package_assets")
    op.drop_index("ix_material_packages_tenant_product_id", table_name="material_packages")
    op.drop_table("material_packages")
    op.drop_index("ix_source_images_tenant_product_id", table_name="source_images")
    op.drop_table("source_images")
    op.drop_index("ix_products_tenant_product_code", table_name="products")
    op.drop_table("products")
    op.drop_index(
        "ix_outbox_events_tenant_status_available",
        table_name="outbox_events",
    )
    op.drop_table("outbox_events")
    op.drop_table("inbox_events")
