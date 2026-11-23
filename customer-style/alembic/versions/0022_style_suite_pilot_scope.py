"""Persist pilot idempotency scope alongside sealed suite items."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_style_suite_pilot_scope"
down_revision: str | None = "0021_style_suite_fidelity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "style_suite_runs", sa.Column("pilot_scope", sa.JSON(), nullable=True)
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_runs_pilot_scope_immutable "
        "BEFORE UPDATE OF pilot_scope ON style_suite_runs "
        "WHEN NEW.pilot_scope IS NOT OLD.pilot_scope "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_runs_immutable'); END"
    )


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM style_suite_runs WHERE pilot_scope IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError("cannot discard sealed style suite pilot scopes")
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_runs_pilot_scope_immutable")
    op.drop_column("style_suite_runs", "pilot_scope")
