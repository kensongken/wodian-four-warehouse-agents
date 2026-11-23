"""Add sales strategy V2 and separate handoff ledgers.

Revision ID: 0005_sales_strategy_v2_handoffs
Revises: 0004_classification_ledger
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_sales_strategy_v2_handoffs"
down_revision: str | None = "0004_classification_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "sales_strategy_inputs_v2",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_material_package_version", sa.Integer(), nullable=False),
        sa.Column("source_classification_id", UUID, nullable=False),
        sa.Column("source_classification_version", sa.Integer(), nullable=False),
        sa.Column("source_classification_sha256", sa.String(64), nullable=False),
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
            name="ck_sales_strategy_inputs_v2_preview_only",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_sales_strategy_inputs_v2_product"
        ),
        sa.ForeignKeyConstraint(
            ["source_classification_id"],
            ["product_classification_versions.id"],
            name="fk_sales_strategy_inputs_v2_classification",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sales_strategy_inputs_v2"),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_sales_strategy_inputs_v2_tenant_product_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_sales_strategy_inputs_v2_tenant_product_content",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source_classification_id",
            name="uq_sales_strategy_inputs_v2_tenant_classification",
        ),
    )
    op.create_index(
        "ix_sales_strategy_inputs_v2_tenant_product_version",
        "sales_strategy_inputs_v2",
        ["tenant_id", "product_id", "version"],
    )

    op.create_table(
        "warehouse_handoffs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("strategy_input_id", UUID, nullable=False),
        sa.Column("target_warehouse", sa.String(24), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("receiver_receipt_id", UUID, nullable=True),
        sa.Column("rejection_code", sa.String(64), nullable=True),
        sa.Column(
            "prepared_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_warehouse = 'sales'",
            name="ck_warehouse_handoffs_sales",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'pending', 'accepted', 'rejected')",
            name="ck_warehouse_handoffs_status",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_warehouse_handoffs_product"
        ),
        sa.ForeignKeyConstraint(
            ["strategy_input_id"],
            ["sales_strategy_inputs_v2.id"],
            name="fk_warehouse_handoffs_strategy_input",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_warehouse_handoffs"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_warehouse_handoffs_tenant_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "strategy_input_id",
            "target_warehouse",
            name="uq_warehouse_handoffs_tenant_input_target",
        ),
    )
    op.create_index(
        "ix_warehouse_handoffs_tenant_status",
        "warehouse_handoffs",
        ["tenant_id", "status"],
    )

    op.create_table(
        "sales_warehouse_receipts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("handoff_id", UUID, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("accepted_payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["handoff_id"],
            ["warehouse_handoffs.id"],
            name="fk_sales_warehouse_receipts_handoff",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sales_warehouse_receipts"),
        sa.UniqueConstraint(
            "tenant_id",
            "handoff_id",
            name="uq_sales_warehouse_receipts_tenant_handoff",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_sales_warehouse_receipts_tenant_key",
        ),
    )
    op.create_index(
        "ix_sales_warehouse_receipts_tenant_accepted",
        "sales_warehouse_receipts",
        ["tenant_id", "accepted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sales_warehouse_receipts_tenant_accepted",
        table_name="sales_warehouse_receipts",
    )
    op.drop_table("sales_warehouse_receipts")
    op.drop_index(
        "ix_warehouse_handoffs_tenant_status",
        table_name="warehouse_handoffs",
    )
    op.drop_table("warehouse_handoffs")
    op.drop_index(
        "ix_sales_strategy_inputs_v2_tenant_product_version",
        table_name="sales_strategy_inputs_v2",
    )
    op.drop_table("sales_strategy_inputs_v2")
