"""Add append-only foreground disagreement evidence."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046_foreground_disagreement_evidence"
down_revision: str | None = "0045_pixel_review_state_compatibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_foreground_disagreement_evidence",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("foreground_version_id", sa.String(36), nullable=False),
        sa.Column("coarse_mask_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(coarse_mask_sha256) = 64",
            name="ck_foreground_disagreement_digest",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "foreground_version_id"],
            ["product_foreground_versions.customer_id", "product_foreground_versions.id"],
            name="fk_foreground_disagreement_customer_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id",
            "foreground_version_id",
            name="uq_foreground_disagreement_customer_version",
        ),
    )
    op.execute(
        "CREATE TRIGGER trg_foreground_disagreement_update "
        "BEFORE UPDATE ON product_foreground_disagreement_evidence "
        "BEGIN SELECT RAISE(ABORT, 'foreground_disagreement_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_foreground_disagreement_delete "
        "BEFORE DELETE ON product_foreground_disagreement_evidence "
        "BEGIN SELECT RAISE(ABORT, 'foreground_disagreement_immutable'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT count(*) FROM product_foreground_disagreement_evidence")
    ).scalar_one()
    if count:
        raise RuntimeError("cannot discard foreground disagreement evidence")
    op.execute("DROP TRIGGER trg_foreground_disagreement_delete")
    op.execute("DROP TRIGGER trg_foreground_disagreement_update")
    op.drop_table("product_foreground_disagreement_evidence")
