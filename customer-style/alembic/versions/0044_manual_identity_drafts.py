"""Allow zero-cost local manual product identity drafts."""

from collections.abc import Sequence

from alembic import op

revision: str = "0044_manual_identity_drafts"
down_revision: str | None = "0043_unified_style_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("product_identity_analysis_runs") as batch:
        batch.drop_constraint(
            "ck_product_identity_analysis_usd_positive", type_="check"
        )
        batch.create_check_constraint(
            "ck_product_identity_analysis_usd_nonnegative",
            "authorized_usd >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("product_identity_analysis_runs") as batch:
        batch.drop_constraint(
            "ck_product_identity_analysis_usd_nonnegative", type_="check"
        )
        batch.create_check_constraint(
            "ck_product_identity_analysis_usd_positive",
            "authorized_usd > 0",
        )
