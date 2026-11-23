"""Add append-only material evidence provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_material_evidence"
down_revision: str | None = "0006_style_warehouse_v2_handoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "material_evidence_uploads",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('staged', 'consumed')",
            name="ck_material_evidence_upload_status",
        ),
        sa.CheckConstraint(
            "(status = 'staged' AND consumed_at IS NULL) OR "
            "(status = 'consumed' AND consumed_at IS NOT NULL)",
            name="ck_material_evidence_upload_consumption",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "evidence_ref",
            name="uq_material_evidence_upload_tenant_ref",
        ),
    )
    op.create_index(
        "ix_material_evidence_uploads_tenant_product_status",
        "material_evidence_uploads",
        ["tenant_id", "product_id", "status"],
        unique=False,
    )

    op.create_table(
        "material_evidence_records",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("base_classification_id", UUID, nullable=False),
        sa.Column("material_label", sa.String(length=128), nullable=False),
        sa.Column("claim_basis", sa.String(length=32), nullable=False),
        sa.Column("evidence_kind", sa.String(length=32), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_summary", sa.String(length=500), nullable=False),
        sa.Column("declared_by_ref", sa.String(length=128), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewer_ref", sa.String(length=128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", UUID, nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "claim_basis IN ('document_supported', 'merchant_declared')",
            name="ck_material_evidence_claim_basis",
        ),
        sa.CheckConstraint(
            "evidence_kind IN ('inspection_certificate', 'purchase_record', "
            "'supplier_label', 'merchant_declaration', 'other_document')",
            name="ck_material_evidence_kind",
        ),
        sa.CheckConstraint(
            "(claim_basis = 'merchant_declared' "
            "AND evidence_kind = 'merchant_declaration') OR "
            "(claim_basis = 'document_supported' "
            "AND evidence_kind <> 'merchant_declaration' "
            "AND evidence_sha256 IS NOT NULL)",
            name="ck_material_evidence_basis_kind",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["base_classification_id"], ["product_classification_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            name="uq_material_evidence_tenant_request",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_material_evidence_tenant_product_content",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "evidence_ref",
            name="uq_material_evidence_tenant_ref",
        ),
    )
    op.create_index(
        "ix_material_evidence_tenant_product_created",
        "material_evidence_records",
        ["tenant_id", "product_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "material_evidence_revocations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("evidence_id", UUID, nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("reason_summary", sa.String(length=500), nullable=False),
        sa.Column("revoked_by_ref", sa.String(length=128), nullable=False),
        sa.Column("request_id", UUID, nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason_code IN ('merchant_withdrawn', 'document_invalidated', "
            "'wrong_product', 'other')",
            name="ck_material_evidence_revocation_reason",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["evidence_id"], ["material_evidence_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "evidence_id",
            name="uq_material_evidence_revocations_tenant_evidence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            name="uq_material_evidence_revocations_tenant_request",
        ),
    )
    op.create_index(
        "ix_material_evidence_revocations_tenant_evidence",
        "material_evidence_revocations",
        ["tenant_id", "evidence_id"],
        unique=False,
    )

    op.add_column(
        "product_classification_versions",
        sa.Column("material_claim_basis", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "product_classification_versions",
        sa.Column("material_evidence_id", UUID, nullable=True),
    )
    op.create_foreign_key(
        "fk_classification_versions_material_evidence",
        "product_classification_versions",
        "material_evidence_records",
        ["material_evidence_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_classification_versions_material_provenance",
        "product_classification_versions",
        "(material_claim_basis IS NULL AND material_evidence_id IS NULL) OR "
        "(material_claim_basis = 'information_insufficient' "
        "AND material_evidence_id IS NULL) OR "
        "(material_claim_basis IN ('document_supported', 'merchant_declared') "
        "AND material_evidence_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_classification_versions_material_provenance",
        "product_classification_versions",
        type_="check",
    )
    op.drop_constraint(
        "fk_classification_versions_material_evidence",
        "product_classification_versions",
        type_="foreignkey",
    )
    op.drop_column("product_classification_versions", "material_evidence_id")
    op.drop_column("product_classification_versions", "material_claim_basis")

    op.drop_index(
        "ix_material_evidence_revocations_tenant_evidence",
        table_name="material_evidence_revocations",
    )
    op.drop_table("material_evidence_revocations")
    op.drop_index(
        "ix_material_evidence_tenant_product_created",
        table_name="material_evidence_records",
    )
    op.drop_table("material_evidence_records")
    op.drop_index(
        "ix_material_evidence_uploads_tenant_product_status",
        table_name="material_evidence_uploads",
    )
    op.drop_table("material_evidence_uploads")
