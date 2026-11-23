"""Seal provider-sized historical style reference packs."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_style_reference_packs"
down_revision: str | None = "0031_style_suite_identity_card"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sqlite_immutable(table_name: str) -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(
        f"CREATE TRIGGER trg_{table_name}_immutable BEFORE UPDATE ON {table_name} "
        f"BEGIN SELECT RAISE(ABORT, '{table_name}_immutable'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table_name}_no_delete BEFORE DELETE ON {table_name} "
        f"BEGIN SELECT RAISE(ABORT, '{table_name}_immutable'); END"
    )


def upgrade() -> None:
    op.create_table(
        "style_reference_packs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "customer_id",
            sa.String(36),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column("scene_style_version_id", sa.String(36), nullable=False),
        sa.Column("selection_version", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("confirmed_by", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id", "scene_style_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_reference_packs_customer_version",
        ),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_style_reference_packs_customer_id"
        ),
        sa.UniqueConstraint(
            "scene_style_version_id",
            "manifest_sha256",
            name="uq_style_reference_packs_version_manifest",
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_style_reference_packs_manifest_sha256",
        ),
        sa.CheckConstraint(
            "length(trim(selection_version)) > 0",
            name="ck_style_reference_packs_selection_version",
        ),
        sa.CheckConstraint(
            "length(trim(confirmed_by)) > 0",
            name="ck_style_reference_packs_confirmed_by",
        ),
    )
    op.create_index(
        "ix_style_reference_packs_customer_id",
        "style_reference_packs",
        ["customer_id"],
    )
    op.create_index(
        "ix_style_reference_packs_scene_style_version_id",
        "style_reference_packs",
        ["scene_style_version_id"],
    )
    op.create_table(
        "style_reference_pack_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "customer_id",
            sa.String(36),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column("pack_id", sa.String(36), nullable=False),
        sa.Column("style_sample_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id", "pack_id"],
            ["style_reference_packs.customer_id", "style_reference_packs.id"],
            name="fk_style_reference_pack_items_customer_pack",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_sample_id"],
            ["style_samples.customer_id", "style_samples.id"],
            name="fk_style_reference_pack_items_customer_sample",
        ),
        sa.UniqueConstraint(
            "pack_id", "position", name="uq_style_reference_pack_items_position"
        ),
        sa.UniqueConstraint(
            "pack_id", "style_sample_id", name="uq_style_reference_pack_items_sample"
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 3",
            name="ck_style_reference_pack_items_position",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_style_reference_pack_items_content_sha256",
        ),
        sa.CheckConstraint(
            "mime_type IN ('image/jpeg','image/png')",
            name="ck_style_reference_pack_items_mime_type",
        ),
    )
    op.create_index(
        "ix_style_reference_pack_items_customer_id",
        "style_reference_pack_items",
        ["customer_id"],
    )
    op.create_index(
        "ix_style_reference_pack_items_pack_id",
        "style_reference_pack_items",
        ["pack_id"],
    )
    _sqlite_immutable("style_reference_packs")
    _sqlite_immutable("style_reference_pack_items")


def downgrade() -> None:
    count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM style_reference_packs")
    ).scalar_one()
    if count:
        raise RuntimeError("cannot discard sealed style reference packs")
    for table_name in ("style_reference_pack_items", "style_reference_packs"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable")
    op.drop_table("style_reference_pack_items")
    op.drop_table("style_reference_packs")
