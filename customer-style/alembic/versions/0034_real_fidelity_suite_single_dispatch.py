"""Enforce one paid fidelity dispatch ledger per suite run."""

from collections.abc import Sequence

from alembic import op

revision: str = "0034_real_fidelity_suite_single_dispatch"
down_revision: str | None = "0033_style_suite_batch_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_real_fidelity_customer_suite",
        "real_fidelity_runs",
        ["customer_id", "style_suite_run_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_real_fidelity_customer_suite",
        table_name="real_fidelity_runs",
    )
