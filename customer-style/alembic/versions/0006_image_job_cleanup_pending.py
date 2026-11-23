"""Add an auditable cleanup-pending image job recovery state."""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_image_job_cleanup_pending"
down_revision: str | None = "0005_product_asset_intake_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_status_trigger(*, cleanup_pending: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_status_transition")
    recovery_rules = (
        "'machine_check', 'generation_failed', 'cleanup_pending', 'disabled')) "
        "OR (OLD.status = 'cleanup_pending' AND NEW.status IN "
        "('generation_failed')) "
        if cleanup_pending
        else "'machine_check', 'generation_failed', 'disabled')) "
    )
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_status_transition "
        "BEFORE UPDATE OF status ON image_output_jobs "
        "WHEN NEW.status IS NOT OLD.status AND NOT ("
        "(OLD.status = 'queued' AND NEW.status IN "
        "('generating', 'insufficient_information', 'permission_unknown', 'disabled')) "
        "OR (OLD.status = 'generating' AND NEW.status IN ("
        + recovery_rules
        + "OR (OLD.status = 'machine_check' AND NEW.status IN "
        "('awaiting_customer_review', 'authenticity_failed', 'disabled')) "
        "OR (OLD.status = 'awaiting_customer_review' AND NEW.status IN "
        "('customer_approved', 'customer_rejected', 'disabled'))"
        ") BEGIN SELECT RAISE(ABORT, "
        "'invalid_image_output_job_status_transition'); END"
    )


def upgrade() -> None:
    """Permit generating jobs to disclose incomplete artifact cleanup."""
    _replace_status_trigger(cleanup_pending=True)


def downgrade() -> None:
    """Restore the pre-recovery transition guard."""
    pending_count = op.get_bind().execute(
        text(
            "SELECT count(*) FROM image_output_jobs "
            "WHERE status IN ('cleanup_pending', 'cleanup_abandoned')"
        )
    ).scalar_one()
    if pending_count:
        raise RuntimeError(
            "cannot downgrade 0006 while cleanup_pending jobs require recovery"
        )
    _replace_status_trigger(cleanup_pending=False)
