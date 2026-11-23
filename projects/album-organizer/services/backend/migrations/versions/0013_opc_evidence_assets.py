"""add immutable OPC evidence asset registry

Revision ID: 0013_opc_evidence_assets
Revises: 0012_album_product_snapshots
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_opc_evidence_assets"
down_revision = "0012_album_product_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opc_evidence_assets",
        sa.Column("opc_id", sa.String(length=128), nullable=False),
        sa.Column("source_asset_id", sa.Uuid(), nullable=False),
        sa.Column("source_blob_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("storage_ref", sa.String(length=512), nullable=False),
        sa.Column("source_object_key", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_opc_evidence_assets_sha256_lowercase_hex"),
        ),
        sa.CheckConstraint(
            "byte_size BETWEEN 1 AND 104857600",
            name=op.f("ck_opc_evidence_assets_byte_size_allowed"),
        ),
        sa.CheckConstraint(
            "media_type IN ('image/jpeg', 'image/png', 'image/heic', 'image/heif')",
            name=op.f("ck_opc_evidence_assets_media_type_allowed"),
        ),
        sa.CheckConstraint(
            "storage_ref ~ '^evidence/[0-9a-f-]{36}/original$'",
            name=op.f("ck_opc_evidence_assets_storage_ref_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ("opc_id",),
            ("opc_workspaces.id",),
            name=op.f("fk_opc_evidence_assets_opc_id_opc_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("source_asset_id",),
            ("assets.id",),
            name=op.f("fk_opc_evidence_assets_source_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("source_blob_id",),
            ("stored_blobs.id",),
            name=op.f("fk_opc_evidence_assets_source_blob_id_stored_blobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("created_by",),
            ("users.id",),
            name=op.f("fk_opc_evidence_assets_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opc_evidence_assets")),
        sa.UniqueConstraint(
            "source_asset_id",
            name="opc_evidence_assets_source_asset_key",
        ),
        sa.UniqueConstraint(
            "storage_ref",
            name="opc_evidence_assets_storage_ref_key",
        ),
    )
    op.create_index(
        "ix_opc_evidence_assets_opc_id",
        "opc_evidence_assets",
        ("opc_id",),
        unique=False,
    )


def downgrade() -> None:
    has_data = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM opc_evidence_assets LIMIT 1"))
        .scalar()
    )
    if has_data is not None:
        raise RuntimeError(
            "cannot downgrade 0013_opc_evidence_assets: "
            "opc_evidence_assets contains data; export or explicitly delete it first"
        )
    op.drop_index("ix_opc_evidence_assets_opc_id", table_name="opc_evidence_assets")
    op.drop_table("opc_evidence_assets")
