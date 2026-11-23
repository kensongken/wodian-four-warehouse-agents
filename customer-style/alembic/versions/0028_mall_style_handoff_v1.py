"""Add Mall SaaS style handoff receipts and source bindings."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_mall_style_handoff_v1"
down_revision: str | None = "0027_style_warehouse_v3_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mall_style_handoff_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("handoff_id", sa.String(36), nullable=False),
        sa.Column("merchant_id", sa.String(36), nullable=False),
        sa.Column("opc_id", sa.String(36), nullable=False),
        sa.Column("product_entry_submission_id", sa.String(36), nullable=False),
        sa.Column("product_entry_draft_version", sa.Integer(), nullable=False),
        sa.Column("product_entry_draft_sha256", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("checked_payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=True),
        sa.Column("subject_seal_id", sa.String(36), nullable=True),
        sa.Column("scene_style_id", sa.String(36), nullable=False),
        sa.Column("style_card_version_id", sa.String(36), nullable=False),
        sa.Column("slot_key", sa.String(64), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("maximum_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("generation_job_id", sa.String(36), nullable=True),
        sa.Column("publishable", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "merchant_id",
            "opc_id",
            "idempotency_key",
            name="uq_mall_style_tenant_key",
        ),
        sa.UniqueConstraint(
            "merchant_id",
            "opc_id",
            "handoff_id",
            name="uq_mall_style_tenant_handoff",
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_product_photo','awaiting_subject_confirmation',"
            "'awaiting_generation_approval')",
            name="ck_mall_style_receipt_status",
        ),
        sa.CheckConstraint("generation_job_id IS NULL", name="ck_mall_style_no_job"),
        sa.CheckConstraint("publishable = 0", name="ck_mall_style_not_publishable"),
    )
    op.create_index(
        "ix_mall_style_merchant_received",
        "mall_style_handoff_receipts",
        ["merchant_id", "received_at"],
    )
    op.create_table(
        "mall_style_source_asset_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("merchant_id", sa.String(36), nullable=False),
        sa.Column("opc_id", sa.String(36), nullable=False),
        sa.Column("source_asset_id", sa.String(36), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("asset_role", sa.String(32), nullable=False),
        sa.Column("style_product_asset_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "merchant_id",
            "opc_id",
            "source_asset_id",
            name="uq_mall_style_source_asset",
        ),
        sa.CheckConstraint(
            "asset_role IN ('label_photo','product_photo')",
            name="ck_mall_style_source_asset_role",
        ),
    )
    op.create_index(
        "ix_mall_style_source_merchant",
        "mall_style_source_asset_bindings",
        ["merchant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mall_style_source_merchant",
        table_name="mall_style_source_asset_bindings",
    )
    op.drop_table("mall_style_source_asset_bindings")
    op.drop_index(
        "ix_mall_style_merchant_received", table_name="mall_style_handoff_receipts"
    )
    op.drop_table("mall_style_handoff_receipts")
