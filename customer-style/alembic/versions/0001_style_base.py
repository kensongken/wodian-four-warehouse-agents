"""Create the customer-isolated style persistence base."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_style_base"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create customer-owned style tables using SQLite-portable UUID strings."""
    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "style_samples",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("photo_id", sa.String(length=255), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_provenance", sa.String(length=100), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "id", name="uq_style_samples_customer_id_id"),
    )
    op.create_index("ix_style_samples_customer_id", "style_samples", ["customer_id"])
    op.create_index(
        "uq_style_samples_customer_photo",
        "style_samples",
        ["customer_id", "photo_id"],
        unique=True,
    )
    op.create_table(
        "style_cards",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "id", name="uq_style_cards_customer_id_id"),
    )
    op.create_index("ix_style_cards_customer_id", "style_cards", ["customer_id"])
    op.create_index(
        "uq_style_cards_active_main_customer",
        "style_cards",
        ["customer_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'main' AND active = 1"),
    )
    op.create_table(
        "style_card_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("style_card_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_card_id"],
            ["style_cards.customer_id", "style_cards.id"],
            name="fk_style_card_versions_customer_style_card",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_style_card_versions_customer_id_id"
        ),
    )
    op.create_index(
        "ix_style_card_versions_customer_id", "style_card_versions", ["customer_id"]
    )
    op.create_index(
        "ix_style_card_versions_style_card_id", "style_card_versions", ["style_card_id"]
    )
    op.create_index(
        "uq_style_card_versions_card_version",
        "style_card_versions",
        ["style_card_id", "version"],
        unique=True,
    )
    op.execute(
        "CREATE TRIGGER trg_style_card_versions_no_update "
        "BEFORE UPDATE ON style_card_versions "
        "BEGIN SELECT RAISE(ABORT, 'style_card_version_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_card_versions_no_delete "
        "BEFORE DELETE ON style_card_versions "
        "BEGIN SELECT RAISE(ABORT, 'style_card_version_immutable'); END"
    )
    op.create_table(
        "style_card_sample_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("style_card_version_id", sa.String(length=36), nullable=False),
        sa.Column("style_sample_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_card_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_card_sample_links_customer_version",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_sample_id"],
            ["style_samples.customer_id", "style_samples.id"],
            name="fk_style_card_sample_links_customer_sample",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_style_card_sample_links_customer_id",
        "style_card_sample_links",
        ["customer_id"],
    )
    op.create_index(
        "ix_style_card_sample_links_style_card_version_id",
        "style_card_sample_links",
        ["style_card_version_id"],
    )
    op.create_index(
        "ix_style_card_sample_links_style_sample_id",
        "style_card_sample_links",
        ["style_sample_id"],
    )
    op.create_index(
        "uq_style_card_sample_link",
        "style_card_sample_links",
        ["style_card_version_id", "style_sample_id"],
        unique=True,
    )
    op.create_table(
        "scene_styles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("style_card_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_card_id"],
            ["style_cards.customer_id", "style_cards.id"],
            name="fk_scene_styles_customer_style_card",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scene_styles_customer_id", "scene_styles", ["customer_id"])
    op.create_index("ix_scene_styles_style_card_id", "scene_styles", ["style_card_id"])
    op.create_index(
        "uq_scene_styles_active_customer_name",
        "scene_styles",
        ["customer_id", "name"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_customer_id", "audit_events", ["customer_id"])


def downgrade() -> None:
    """Remove the style persistence base in reverse dependency order."""
    op.drop_index("ix_audit_events_customer_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("uq_scene_styles_active_customer_name", table_name="scene_styles")
    op.drop_index("ix_scene_styles_style_card_id", table_name="scene_styles")
    op.drop_index("ix_scene_styles_customer_id", table_name="scene_styles")
    op.drop_table("scene_styles")
    op.drop_index("uq_style_card_sample_link", table_name="style_card_sample_links")
    op.execute("DROP TRIGGER trg_style_card_versions_no_delete")
    op.execute("DROP TRIGGER trg_style_card_versions_no_update")
    op.drop_index(
        "ix_style_card_sample_links_style_sample_id", table_name="style_card_sample_links"
    )
    op.drop_index(
        "ix_style_card_sample_links_style_card_version_id",
        table_name="style_card_sample_links",
    )
    op.drop_index(
        "ix_style_card_sample_links_customer_id", table_name="style_card_sample_links"
    )
    op.drop_table("style_card_sample_links")
    op.drop_index(
        "uq_style_card_versions_card_version", table_name="style_card_versions"
    )
    op.drop_index(
        "ix_style_card_versions_style_card_id", table_name="style_card_versions"
    )
    op.drop_index(
        "ix_style_card_versions_customer_id", table_name="style_card_versions"
    )
    op.drop_table("style_card_versions")
    op.drop_index("uq_style_cards_active_main_customer", table_name="style_cards")
    op.drop_index("ix_style_cards_customer_id", table_name="style_cards")
    op.drop_table("style_cards")
    op.drop_index("uq_style_samples_customer_photo", table_name="style_samples")
    op.drop_index("ix_style_samples_customer_id", table_name="style_samples")
    op.drop_table("style_samples")
    op.drop_table("customers")
