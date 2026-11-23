"""Allow one-way human acknowledgement without mutating machine fidelity."""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_fidelity_warning_acknowledgement"
down_revision: str | None = "0028_real_fidelity_authorization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER trg_style_suite_candidate_fidelity_no_update")
    op.execute(
        "CREATE TRIGGER trg_style_suite_candidate_fidelity_no_update BEFORE UPDATE ON style_suite_candidate_fidelities "
        "WHEN NOT (OLD.customer_id=NEW.customer_id AND OLD.run_item_id=NEW.run_item_id "
        "AND OLD.output_job_id=NEW.output_job_id AND OLD.image_candidate_id=NEW.image_candidate_id "
        "AND OLD.fidelity_state=NEW.fidelity_state AND OLD.fidelity_flags=NEW.fidelity_flags "
        "AND OLD.summary_zh IS NEW.summary_zh AND OLD.created_at=NEW.created_at "
        "AND OLD.warning_acknowledged=0 AND NEW.warning_acknowledged=1) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_candidate_fidelity_immutable'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_style_suite_candidate_fidelity_no_update")
    op.execute(
        "CREATE TRIGGER trg_style_suite_candidate_fidelity_no_update BEFORE UPDATE ON style_suite_candidate_fidelities "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_candidate_fidelity_immutable'); END"
    )
