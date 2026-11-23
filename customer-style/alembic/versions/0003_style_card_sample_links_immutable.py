"""Protect style-card evidence links from updates and deletes."""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_style_card_sample_links_immutable"
down_revision: str | None = "0002_audit_events_immutable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make style-card evidence associations append-only outside the ORM too."""
    op.execute(
        "CREATE TRIGGER trg_style_card_sample_links_no_update "
        "BEFORE UPDATE ON style_card_sample_links "
        "BEGIN SELECT RAISE(ABORT, 'style_card_sample_link_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_card_sample_links_no_delete "
        "BEFORE DELETE ON style_card_sample_links "
        "BEGIN SELECT RAISE(ABORT, 'style_card_sample_link_immutable'); END"
    )


def downgrade() -> None:
    """Remove evidence-link immutability triggers in reverse order."""
    op.execute("DROP TRIGGER trg_style_card_sample_links_no_delete")
    op.execute("DROP TRIGGER trg_style_card_sample_links_no_update")
