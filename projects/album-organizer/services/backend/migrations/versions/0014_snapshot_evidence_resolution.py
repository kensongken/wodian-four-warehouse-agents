"""mark snapshot evidence resolution cutover

Revision ID: 0014_snapshot_evidence_mode
Revises: 0013_opc_evidence_assets
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_snapshot_evidence_mode"
down_revision = "0013_opc_evidence_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "album_product_snapshots",
        sa.Column(
            "evidence_resolution_mode",
            sa.String(length=32),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE album_product_snapshots "
            "SET evidence_resolution_mode = 'legacy_direct'"
        )
    )
    op.alter_column(
        "album_product_snapshots",
        "evidence_resolution_mode",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default="registry_required",
    )
    op.create_check_constraint(
        op.f("ck_album_product_snapshots_evidence_resolution_mode_allowed"),
        "album_product_snapshots",
        "evidence_resolution_mode IN ('legacy_direct', 'registry_required')",
    )


def downgrade() -> None:
    has_data = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM album_product_snapshots LIMIT 1"))
        .scalar()
    )
    if has_data is not None:
        raise RuntimeError(
            "cannot downgrade 0014_snapshot_evidence_mode: "
            "album_product_snapshots contains evidence resolution data; "
            "export or explicitly delete it first"
        )
    op.drop_constraint(
        op.f("ck_album_product_snapshots_evidence_resolution_mode_allowed"),
        "album_product_snapshots",
        type_="check",
    )
    op.drop_column("album_product_snapshots", "evidence_resolution_mode")
