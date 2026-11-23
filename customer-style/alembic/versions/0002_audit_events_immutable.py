"""Protect audit evidence from direct SQLite updates and deletes."""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_audit_events_immutable"
down_revision: str | None = "0001_style_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make append-only audit evidence immutable outside the ORM too."""
    op.execute(
        "CREATE TRIGGER trg_audit_events_no_update "
        "BEFORE UPDATE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_event_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_no_delete "
        "BEFORE DELETE ON audit_events "
        "BEGIN SELECT RAISE(ABORT, 'audit_event_immutable'); END"
    )


def downgrade() -> None:
    """Remove the audit immutability triggers in reverse order."""
    op.execute("DROP TRIGGER trg_audit_events_no_delete")
    op.execute("DROP TRIGGER trg_audit_events_no_update")
