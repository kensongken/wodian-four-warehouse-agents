"""Add immutable customer-approved dual-image associations."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision: str = "0009_customer_image_review"
down_revision: str | None = "0008_image_job_fencing_capability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _create_supporting_unique_indexes() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_product_assets_customer_id_product "
        "ON product_assets (customer_id, id, product_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_image_reviews_customer_id_candidate "
        "ON image_reviews (customer_id, id, image_candidate_id)"
    )


def _create_review_decision_trigger() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_image_reviews_decision_insert")
    op.execute(
        "CREATE TRIGGER trg_image_reviews_decision_insert "
        "BEFORE INSERT ON image_reviews "
        "WHEN NEW.decision NOT IN "
        "('approved', 'not_my_style', 'product_detail_inaccurate', 'rejected') "
        "BEGIN SELECT RAISE(ABORT, 'invalid_image_review_decision'); END"
    )


def _create_association_triggers() -> None:
    for trigger in (
        "trg_approved_image_associations_no_update",
        "trg_approved_image_associations_no_delete",
        "trg_approved_image_associations_approved_review_only",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_no_update "
        "BEFORE UPDATE ON approved_image_associations "
        "BEGIN SELECT RAISE(ABORT, 'approved_image_association_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_no_delete "
        "BEFORE DELETE ON approved_image_associations "
        "BEGIN SELECT RAISE(ABORT, 'approved_image_association_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_approved_review_only "
        "BEFORE INSERT ON approved_image_associations "
        "WHEN NOT EXISTS (SELECT 1 FROM image_reviews AS review "
        "WHERE review.customer_id = NEW.customer_id "
        "AND review.id = NEW.image_review_id "
        "AND review.image_candidate_id = NEW.image_candidate_id "
        "AND review.decision = 'approved') "
        "BEGIN SELECT RAISE(ABORT, 'association_requires_approved_review'); END"
    )


def upgrade() -> None:
    """Create one immutable association row per approved candidate/source pair."""
    _create_supporting_unique_indexes()
    _create_review_decision_trigger()
    if "approved_image_associations" not in _table_names():
        op.create_table(
            "approved_image_associations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("customer_id", sa.String(length=36), nullable=False),
            sa.Column("image_review_id", sa.String(length=36), nullable=False),
            sa.Column("image_candidate_id", sa.String(length=36), nullable=False),
            sa.Column("source_asset_id", sa.String(length=36), nullable=False),
            sa.Column("product_id", sa.String(length=255), nullable=False),
            sa.Column("image_kind", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "image_kind IN ('real_photo', 'style_image')",
                name="ck_approved_image_associations_image_kind",
            ),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
            sa.ForeignKeyConstraint(
                ["customer_id", "image_candidate_id"],
                ["image_candidates.customer_id", "image_candidates.id"],
                name="fk_approved_image_associations_customer_candidate",
            ),
            sa.ForeignKeyConstraint(
                ["customer_id", "source_asset_id", "product_id"],
                [
                    "product_assets.customer_id",
                    "product_assets.id",
                    "product_assets.product_id",
                ],
                name="fk_approved_image_associations_customer_source_product",
            ),
            sa.ForeignKeyConstraint(
                ["customer_id", "image_review_id", "image_candidate_id"],
                [
                    "image_reviews.customer_id",
                    "image_reviews.id",
                    "image_reviews.image_candidate_id",
                ],
                name="fk_approved_image_associations_customer_review_candidate",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "image_review_id",
                "source_asset_id",
                name="uq_approved_image_associations_review_source",
            ),
        )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_approved_image_associations_candidate_source "
        "ON approved_image_associations (image_candidate_id, source_asset_id)"
    )
    for name, columns in (
        ("ix_approved_image_associations_customer_id", "customer_id"),
        ("ix_approved_image_associations_image_review_id", "image_review_id"),
        ("ix_approved_image_associations_image_candidate_id", "image_candidate_id"),
        ("ix_approved_image_associations_source_asset_id", "source_asset_id"),
        ("ix_approved_image_associations_product_id", "product_id"),
    ):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON approved_image_associations ({columns})"
        )
    _create_association_triggers()


def downgrade() -> None:
    """Refuse to erase an approved association, otherwise remove the contract."""
    if "approved_image_associations" in _table_names():
        count = op.get_bind().execute(
            text("SELECT count(*) FROM approved_image_associations")
        ).scalar_one()
        if count:
            raise RuntimeError(
                "cannot discard customer-approved image associations while rows exist"
            )
        for trigger in (
            "trg_approved_image_associations_approved_review_only",
            "trg_approved_image_associations_no_delete",
            "trg_approved_image_associations_no_update",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        op.drop_table("approved_image_associations")
    op.execute("DROP TRIGGER IF EXISTS trg_image_reviews_decision_insert")
    op.execute("DROP INDEX IF EXISTS uq_image_reviews_customer_id_candidate")
    op.execute("DROP INDEX IF EXISTS uq_product_assets_customer_id_product")
