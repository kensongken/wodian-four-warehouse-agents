"""Move legacy local pixel jobs into the shared customer review gate."""

from collections.abc import Sequence

from alembic import op

revision: str = "0045_pixel_review_state_compatibility"
down_revision: str | None = "0044_manual_identity_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_status_trigger(*, include_legacy_pixel_state: bool) -> None:
    legacy_transition = (
        "OR (OLD.status = 'awaiting_review' AND "
        "NEW.status = 'awaiting_customer_review') "
        if include_legacy_pixel_state
        else ""
    )
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_status_transition")
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_status_transition "
        "BEFORE UPDATE OF status ON image_output_jobs "
        "WHEN NEW.status IS NOT OLD.status AND NOT ("
        "(OLD.status = 'queued' AND NEW.status IN "
        "('generating', 'insufficient_information', 'permission_unknown', 'disabled')) "
        "OR (OLD.status = 'generating' AND NEW.status IN "
        "('machine_check', 'generation_failed', 'generation_uncertain', "
        "'cleanup_pending', 'cleanup_abandoned', 'disabled')) "
        "OR (OLD.status = 'cleanup_pending' AND NEW.status IN "
        "('generation_failed', 'cleanup_abandoned')) "
        "OR (OLD.status = 'generation_failed' AND NEW.status = 'generating') "
        "OR (OLD.status = 'machine_check' AND NEW.status IN "
        "('awaiting_customer_review', 'authenticity_failed', 'disabled')) "
        "OR (OLD.status = 'awaiting_customer_review' AND NEW.status IN "
        "('customer_approved', 'customer_rejected', 'disabled')) "
        + legacy_transition
        + ") BEGIN SELECT RAISE(ABORT, "
        "'invalid_image_output_job_status_transition'); END"
    )


def upgrade() -> None:
    _create_status_trigger(include_legacy_pixel_state=True)


def downgrade() -> None:
    _create_status_trigger(include_legacy_pixel_state=False)
