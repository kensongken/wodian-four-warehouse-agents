"""Create immutable preview sales strategy inputs.

Revision ID: 0003_sales_strategy_inputs
Revises: 0002_tenant_scope_inbox_events
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_sales_strategy_inputs"
down_revision: str | None = "0002_tenant_scope_inbox_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "sales_strategy_inputs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("product_data_version", sa.Integer(), nullable=False),
        sa.Column("source_album_event_id", UUID, nullable=False),
        sa.Column("source_material_package_version", sa.Integer(), nullable=False),
        sa.Column("source_image_set_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status = 'preview_only'",
            name="ck_sales_strategy_inputs_preview_only",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_sales_strategy_inputs_product"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sales_strategy_inputs"),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_sales_strategy_inputs_tenant_product_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_sales_strategy_inputs_tenant_product_content",
        ),
    )
    op.create_index(
        "ix_sales_strategy_inputs_tenant_product_version",
        "sales_strategy_inputs",
        ["tenant_id", "product_id", "version"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sales_strategy_inputs_tenant_product_version",
        table_name="sales_strategy_inputs",
    )
    op.drop_table("sales_strategy_inputs")
