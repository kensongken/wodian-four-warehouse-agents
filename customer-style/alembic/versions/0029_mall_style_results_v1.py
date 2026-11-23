"""Add the Mall generation approval queue and result lineage."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_mall_style_results_v1"
down_revision: str | None = "0028_mall_style_handoff_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mall_style_subject_confirmations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "receipt_id",
            sa.String(36),
            sa.ForeignKey("mall_style_handoff_receipts.id"),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.String(36), nullable=False),
        sa.Column("opc_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("base_subject_seal_id", sa.String(36), nullable=False),
        sa.Column("base_mask_sha256", sa.String(64), nullable=False),
        sa.Column("proposal_version", sa.Integer(), nullable=False),
        sa.Column("reviewed_mask_sha256", sa.String(64), nullable=False),
        sa.Column("reviewed_subject_seal_id", sa.String(36), nullable=False),
        sa.Column("owner_actor_reference", sa.String(255), nullable=False),
        sa.Column("actor_role", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("confirmation_payload_sha256", sa.String(64), nullable=False),
        sa.Column("audit_event_id", sa.String(36), nullable=False),
        sa.Column("publishable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("receipt_id", name="uq_mall_style_subject_receipt"),
        sa.UniqueConstraint(
            "merchant_id",
            "opc_id",
            "idempotency_key",
            name="uq_mall_style_subject_tenant_key",
        ),
        sa.CheckConstraint(
            "proposal_version = 1", name="ck_mall_style_subject_version"
        ),
        sa.CheckConstraint(
            "actor_role = 'merchant_owner'", name="ck_mall_style_subject_owner"
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64 AND length(base_mask_sha256) = 64 "
            "AND length(reviewed_mask_sha256) = 64 "
            "AND length(confirmation_payload_sha256) = 64",
            name="ck_mall_style_subject_digests",
        ),
        sa.CheckConstraint(
            "publishable = 0", name="ck_mall_style_subject_not_publishable"
        ),
    )
    op.create_table(
        "mall_style_generation_queue_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "receipt_id",
            sa.String(36),
            sa.ForeignKey("mall_style_handoff_receipts.id"),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.String(36), nullable=False),
        sa.Column("opc_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("approval_reference", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(255), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("owner_actor_reference", sa.String(255), nullable=False),
        sa.Column("actor_role", sa.String(32), nullable=False),
        sa.Column("approval_audit_event_id", sa.String(36), nullable=False),
        sa.Column("approval_payload_sha256", sa.String(64), nullable=False),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rights_permission_event_id", sa.String(36), nullable=False),
        sa.Column("rights_reference", sa.String(255), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("subject_seal_id", sa.String(36), nullable=False),
        sa.Column("scene_style_id", sa.String(36), nullable=False),
        sa.Column("style_card_version_id", sa.String(36), nullable=False),
        sa.Column("slot_key", sa.String(64), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("maximum_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "generation_job_id",
            sa.String(36),
            sa.ForeignKey("image_output_jobs.id"),
            nullable=True,
        ),
        sa.Column("failure_reason", sa.String(255), nullable=True),
        sa.Column("publishable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("receipt_id", name="uq_mall_style_generation_receipt"),
        sa.UniqueConstraint(
            "merchant_id",
            "opc_id",
            "idempotency_key",
            name="uq_mall_style_generation_tenant_key",
        ),
        sa.UniqueConstraint("generation_job_id", name="uq_mall_style_generation_job"),
        sa.CheckConstraint(
            "status IN ('pending_execution','dispatched','failed','revoked','expired')",
            name="ck_mall_style_generation_status",
        ),
        sa.CheckConstraint(
            "candidate_count BETWEEN 1 AND 3",
            name="ck_mall_style_generation_candidate_count",
        ),
        sa.CheckConstraint(
            "actor_role = 'merchant_owner'", name="ck_mall_style_generation_owner"
        ),
        sa.CheckConstraint(
            "maximum_cost_usd >= 0", name="ck_mall_style_generation_cost"
        ),
        sa.CheckConstraint(
            "length(approval_payload_sha256) = 64",
            name="ck_mall_style_generation_payload_sha",
        ),
        sa.CheckConstraint(
            "publishable = 0", name="ck_mall_style_generation_not_publishable"
        ),
    )
    op.create_index(
        "ix_mall_style_generation_tenant_product",
        "mall_style_generation_queue_items",
        ["merchant_id", "opc_id", "product_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mall_style_generation_tenant_product",
        table_name="mall_style_generation_queue_items",
    )
    op.drop_table("mall_style_generation_queue_items")
    op.drop_table("mall_style_subject_confirmations")
