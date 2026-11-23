"""Add style eligibility, authorization, handoff, and decision ledgers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_style_warehouse_v2_handoffs"
down_revision: str | None = "0005_sales_strategy_v2_handoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "style_transform_authorizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorization_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("classification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_image_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=48), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "authorized_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope = 'local_style_acceptance_only'",
            name="ck_style_authorizations_local_scope",
        ),
        sa.ForeignKeyConstraint(["classification_id"], ["product_classification_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "authorization_request_id",
            name="uq_style_authorizations_tenant_request",
        ),
    )
    op.create_index(
        "ix_style_authorizations_tenant_source_scope",
        "style_transform_authorizations",
        ["tenant_id", "classification_id", "source_image_set_sha256", "scope"],
        unique=False,
    )

    op.create_table(
        "style_eligibility_inputs_v2",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_material_package_version", sa.Integer(), nullable=False),
        sa.Column("source_classification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_classification_version", sa.Integer(), nullable=False),
        sa.Column("source_classification_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_image_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("transform_authorization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transform_authorization_status", sa.String(length=16), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status = 'eligibility_only'", name="ck_style_inputs_eligibility_only"),
        sa.CheckConstraint(
            "transform_authorization_status IN ('none', 'active', 'expired', 'revoked')",
            name="ck_style_inputs_authorization_status",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(
            ["source_classification_id"], ["product_classification_versions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["transform_authorization_id"], ["style_transform_authorizations.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_style_inputs_tenant_product_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_style_inputs_tenant_product_content",
        ),
    )
    op.create_index(
        "ix_style_inputs_tenant_product_version",
        "style_eligibility_inputs_v2",
        ["tenant_id", "product_id", "version"],
        unique=False,
    )

    op.create_table(
        "style_warehouse_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("eligibility_input_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("decision_receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rejection_code", sa.String(length=64), nullable=True),
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
            name="ck_style_handoffs_status",
        ),
        sa.ForeignKeyConstraint(["eligibility_input_id"], ["style_eligibility_inputs_v2.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_style_handoffs_tenant_key"),
        sa.UniqueConstraint(
            "tenant_id",
            "eligibility_input_id",
            name="uq_style_handoffs_tenant_input",
        ),
    )
    op.create_index(
        "ix_style_handoffs_tenant_status",
        "style_warehouse_handoffs",
        ["tenant_id", "status"],
        unique=False,
    )

    op.create_table(
        "style_warehouse_decision_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("handoff_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("checked_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("rejection_code", sa.String(length=64), nullable=True),
        sa.Column("generation_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('accepted', 'rejected')", name="ck_style_decisions_decision"
        ),
        sa.CheckConstraint("generation_job_id IS NULL", name="ck_style_decisions_generation_empty"),
        sa.CheckConstraint(
            "(decision = 'rejected' AND rejection_code IS NOT NULL) OR "
            "(decision = 'accepted' AND rejection_code IS NULL)",
            name="ck_style_decisions_rejection_code",
        ),
        sa.ForeignKeyConstraint(["handoff_id"], ["style_warehouse_handoffs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "handoff_id", name="uq_style_decisions_tenant_handoff"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_style_decisions_tenant_key"),
    )
    op.create_index(
        "ix_style_decisions_tenant_decided",
        "style_warehouse_decision_receipts",
        ["tenant_id", "decided_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_style_decisions_tenant_decided",
        table_name="style_warehouse_decision_receipts",
    )
    op.drop_table("style_warehouse_decision_receipts")
    op.drop_index("ix_style_handoffs_tenant_status", table_name="style_warehouse_handoffs")
    op.drop_table("style_warehouse_handoffs")
    op.drop_index(
        "ix_style_inputs_tenant_product_version",
        table_name="style_eligibility_inputs_v2",
    )
    op.drop_table("style_eligibility_inputs_v2")
    op.drop_index(
        "ix_style_authorizations_tenant_source_scope",
        table_name="style_transform_authorizations",
    )
    op.drop_table("style_transform_authorizations")
