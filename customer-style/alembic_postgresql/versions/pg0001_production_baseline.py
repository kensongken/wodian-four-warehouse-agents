"""Frozen PostgreSQL baseline for a NEW dedicated database, with DB audit guards."""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "pg0001_style_production"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Frozen statements separated by a non-SQL sentinel, not naive ';' splitting
    # (PL/pgSQL bodies intentionally contain semicolons).
    sql = Path(__file__).with_name("pg0001_baseline.sql").read_text()
    for statement in sql.split("\n-- statement --\n"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("production_baseline_downgrade_forbidden_restore_backup")
