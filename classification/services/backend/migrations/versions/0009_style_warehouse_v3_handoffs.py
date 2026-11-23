"""Add provenance-aware style V3 handoff tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_style_v3_handoffs"
down_revision: str | None = "0008_sales_v3_handoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "style_eligibility_inputs_v3",
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
            "status = 'eligibility_only'", name="ck_style_inputs_v3_eligibility_only"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(
            ["source_classification_id"], ["product_classification_versions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "product_id", "version", name="uq_style_inputs_v3_tenant_product_version"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_style_inputs_v3_tenant_product_content",
        ),
        sa.UniqueConstraint(
            "tenant_id", "source_classification_id", name="uq_style_inputs_v3_tenant_classification"
        ),
    )
    op.create_index(
        "ix_style_inputs_v3_tenant_product_version",
        "style_eligibility_inputs_v3",
        ["tenant_id", "product_id", "version"],
    )
    op.create_table(
        "style_warehouse_handoffs_v3",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("eligibility_input_id", UUID, nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("decision_receipt_id", UUID, nullable=True),
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
            "status IN ('prepared', 'pending', 'accepted', 'rejected')",
            name="ck_style_handoffs_v3_status",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["eligibility_input_id"], ["style_eligibility_inputs_v3.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_style_handoffs_v3_tenant_key"),
        sa.UniqueConstraint(
            "tenant_id", "eligibility_input_id", name="uq_style_handoffs_v3_tenant_input"
        ),
    )
    op.create_index(
        "ix_style_handoffs_v3_tenant_status", "style_warehouse_handoffs_v3", ["tenant_id", "status"]
    )
    op.create_table(
        "style_warehouse_decision_receipts_v3",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("handoff_id", UUID, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("checked_payload_sha256", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("rejection_code", sa.String(64), nullable=True),
        sa.Column("generation_job_id", UUID, nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('accepted', 'rejected')", name="ck_style_decisions_v3_decision"
        ),
        sa.CheckConstraint(
            "generation_job_id IS NULL", name="ck_style_decisions_v3_generation_empty"
        ),
        sa.CheckConstraint(
            "(decision = 'rejected' AND rejection_code IS NOT NULL) OR "
            "(decision = 'accepted' AND rejection_code IS NULL)",
            name="ck_style_decisions_v3_rejection_code",
        ),
        sa.ForeignKeyConstraint(["handoff_id"], ["style_warehouse_handoffs_v3.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "handoff_id", name="uq_style_decisions_v3_tenant_handoff"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_style_decisions_v3_tenant_key"
        ),
    )
    op.create_index(
        "ix_style_decisions_v3_tenant_decided",
        "style_warehouse_decision_receipts_v3",
        ["tenant_id", "decided_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_style_decisions_v3_tenant_decided", table_name="style_warehouse_decision_receipts_v3"
    )
    op.drop_table("style_warehouse_decision_receipts_v3")
    op.drop_index("ix_style_handoffs_v3_tenant_status", table_name="style_warehouse_handoffs_v3")
    op.drop_table("style_warehouse_handoffs_v3")
    op.drop_index(
        "ix_style_inputs_v3_tenant_product_version", table_name="style_eligibility_inputs_v3"
    )
    op.drop_table("style_eligibility_inputs_v3")
