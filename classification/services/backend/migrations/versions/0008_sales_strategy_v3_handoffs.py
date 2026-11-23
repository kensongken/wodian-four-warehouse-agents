"""Add provenance-aware sales V3 handoff tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_sales_v3_handoffs"
down_revision: str | None = "0007_material_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "sales_strategy_inputs_v3",
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
        sa.CheckConstraint("status = 'preview_only'", name="ck_sales_inputs_v3_preview_only"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(
            ["source_classification_id"], ["product_classification_versions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "product_id", "version", name="uq_sales_inputs_v3_tenant_product_version"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_sales_inputs_v3_tenant_product_content",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source_classification_id",
            name="uq_sales_strategy_inputs_v3_tenant_classification",
        ),
    )
    op.create_index(
        "ix_sales_inputs_v3_tenant_product_version",
        "sales_strategy_inputs_v3",
        ["tenant_id", "product_id", "version"],
    )
    op.create_table(
        "warehouse_handoffs_v3",
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
        sa.CheckConstraint("target_warehouse = 'sales'", name="ck_warehouse_handoffs_v3_sales"),
        sa.CheckConstraint(
            "status IN ('prepared', 'pending', 'accepted', 'rejected')",
            name="ck_warehouse_handoffs_v3_status",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["strategy_input_id"], ["sales_strategy_inputs_v3.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_warehouse_handoffs_v3_tenant_key"
        ),
        sa.UniqueConstraint(
            "tenant_id", "strategy_input_id", name="uq_warehouse_handoffs_v3_tenant_input"
        ),
    )
    op.create_index(
        "ix_warehouse_handoffs_v3_tenant_status", "warehouse_handoffs_v3", ["tenant_id", "status"]
    )
    op.create_table(
        "sales_warehouse_receipts_v3",
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
        sa.ForeignKeyConstraint(["handoff_id"], ["warehouse_handoffs_v3.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "handoff_id", name="uq_sales_receipts_v3_tenant_handoff"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_sales_receipts_v3_tenant_key"),
    )
    op.create_index(
        "ix_sales_receipts_v3_tenant_accepted",
        "sales_warehouse_receipts_v3",
        ["tenant_id", "accepted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sales_receipts_v3_tenant_accepted", table_name="sales_warehouse_receipts_v3")
    op.drop_table("sales_warehouse_receipts_v3")
    op.drop_index("ix_warehouse_handoffs_v3_tenant_status", table_name="warehouse_handoffs_v3")
    op.drop_table("warehouse_handoffs_v3")
    op.drop_index(
        "ix_sales_inputs_v3_tenant_product_version", table_name="sales_strategy_inputs_v3"
    )
    op.drop_table("sales_strategy_inputs_v3")
