"""Join foreground-pipeline and Mall identity-lock migration histories."""

from collections.abc import Sequence

revision: str = "0043_unified_style_heads"
down_revision: tuple[str, str] = (
    "0042_foreground_pipeline_evidence",
    "0030_mall_style_executor",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Both independent histories must already be applied before the join."""


def downgrade() -> None:
    """The join itself creates no tables or columns."""
