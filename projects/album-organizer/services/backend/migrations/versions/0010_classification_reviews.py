"""add classification candidates and review items

Revision ID: 0010_classification_reviews
Revises: 0009_categorization_deliveries
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_classification_reviews"
down_revision = "0009_categorization_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_classifications",
        sa.Column("opc_id", sa.String(length=128), nullable=False),
        sa.Column("album_product_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("input_event_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_event_id", sa.Uuid(), nullable=False),
        sa.Column("input_snapshot_version", sa.Integer(), nullable=False),
        sa.Column("classification_version", sa.Integer(), nullable=False),
        sa.Column("engine_name", sa.String(length=128), nullable=False),
        sa.Column("engine_version", sa.String(length=128), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="requires_review",
            nullable=False,
        ),
        sa.Column("confirmed_level_1", sa.String(length=64), nullable=True),
        sa.Column("confirmed_level_2", sa.String(length=64), nullable=True),
        sa.Column("confirmed_by", sa.String(length=128), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('requires_review', 'confirmed')",
            name=op.f("ck_product_classifications_status_allowed"),
        ),
        sa.CheckConstraint(
            "source_type IN ('first_sale_bead', 'legacy_material', 'redesign_output')",
            name=op.f("ck_product_classifications_source_type_allowed"),
        ),
        sa.CheckConstraint(
            "(status = 'requires_review' AND confirmed_level_1 IS NULL AND confirmed_level_2 IS NULL "
            "AND confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(status = 'confirmed' AND confirmed_level_1 IS NOT NULL AND confirmed_level_2 IS NOT NULL "
            "AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name=op.f("ck_product_classifications_confirmation_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_classifications")),
        sa.UniqueConstraint(
            "candidate_event_id",
            name="product_classifications_candidate_event_key",
        ),
        sa.UniqueConstraint("opc_id", "id", name="product_classifications_opc_id_key"),
        sa.UniqueConstraint(
            "opc_id",
            "album_product_id",
            "input_snapshot_version",
            "classification_version",
            name="product_classifications_snapshot_version_key",
        ),
    )
    op.create_table(
        "classification_review_items",
        sa.Column("opc_id", sa.String(length=128), nullable=False),
        sa.Column("classification_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.String(length=32),
            server_default="classification",
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=32), server_default="open", nullable=False
        ),
        sa.Column("decision_payload", sa.JSON(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
            "kind = 'classification'",
            name=op.f("ck_classification_review_items_kind_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name=op.f("ck_classification_review_items_status_allowed"),
        ),
        sa.CheckConstraint(
            "(status = 'open' AND decision_payload IS NULL AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(status = 'resolved' AND decision_payload IS NOT NULL AND decided_by IS NOT NULL "
            "AND decided_at IS NOT NULL)",
            name=op.f("ck_classification_review_items_decision_state"),
        ),
        sa.ForeignKeyConstraint(
            ("opc_id", "classification_id"),
            ("product_classifications.opc_id", "product_classifications.id"),
            name="classification_reviews_opc_classification_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_classification_review_items")),
        sa.UniqueConstraint(
            "classification_id",
            name="classification_reviews_classification_key",
        ),
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM product_classifications LIMIT 1"))
        .scalar()
        is not None
    ):
        raise RuntimeError(
            "cannot downgrade 0010_classification_reviews: classification review history exists; export or explicitly delete it first"
        )
    op.drop_table("classification_review_items")
    op.drop_table("product_classifications")
