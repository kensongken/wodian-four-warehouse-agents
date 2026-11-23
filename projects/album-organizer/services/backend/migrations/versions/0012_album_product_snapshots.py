"""add immutable Album Product snapshots

Revision ID: 0012_album_product_snapshots
Revises: 0011_opc_memberships
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_album_product_snapshots"
down_revision = "0011_opc_memberships"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "album_product_snapshots",
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("opc_id", sa.String(length=128), nullable=False),
        sa.Column("album_product_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("source_import_task_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("group_confirmed_by", sa.String(length=128), nullable=False),
        sa.Column("group_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_basis", sa.String(length=64), nullable=False),
        sa.Column("allowed_consumers", sa.JSON(), nullable=False),
        sa.Column("images", sa.JSON(), nullable=False),
        sa.Column("source_payload", sa.JSON(), nullable=False),
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
            "snapshot_version >= 1",
            name=op.f("ck_album_product_snapshots_snapshot_version_positive"),
        ),
        sa.CheckConstraint(
            "source_type IN ('first_sale_bead', 'legacy_material', 'redesign_output')",
            name=op.f("ck_album_product_snapshots_source_type_allowed"),
        ),
        sa.CheckConstraint(
            "consent_basis = 'opc_owned_product_material'",
            name=op.f("ck_album_product_snapshots_consent_basis_allowed"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_album_product_snapshots")),
        sa.UniqueConstraint(
            "source_event_id",
            name="album_product_snapshots_source_event_key",
        ),
        sa.UniqueConstraint(
            "opc_id",
            "album_product_id",
            "snapshot_version",
            name="album_product_snapshots_product_version_key",
        ),
    )
    op.create_index(
        "ix_album_product_snapshots_opc_confirmation",
        "album_product_snapshots",
        ("opc_id", sa.text("group_confirmed_at DESC"), sa.text("id DESC")),
        unique=False,
    )
    op.create_index(
        "ix_product_classifications_opc_confirmed_manifest",
        "product_classifications",
        ("opc_id", sa.text("confirmed_at DESC"), sa.text("id DESC")),
        unique=False,
        postgresql_where=sa.text("status = 'confirmed'"),
    )


def downgrade() -> None:
    has_snapshot_data = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM album_product_snapshots LIMIT 1"))
        .scalar()
    )
    if has_snapshot_data is not None:
        raise RuntimeError(
            "cannot downgrade 0012_album_product_snapshots: "
            "album_product_snapshots contains data; export or explicitly "
            "delete it first"
        )
    op.drop_index(
        "ix_product_classifications_opc_confirmed_manifest",
        table_name="product_classifications",
    )
    op.drop_index(
        "ix_album_product_snapshots_opc_confirmation",
        table_name="album_product_snapshots",
    )
    op.drop_table("album_product_snapshots")
