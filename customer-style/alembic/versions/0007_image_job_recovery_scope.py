"""Persist generation claims and artifact cleanup scope."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_image_job_recovery_scope"
down_revision: str | None = "0006_image_job_cleanup_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _assert_no_unscoped_cleanup() -> None:
    count = (
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM image_output_jobs "
                "WHERE status IN ('cleanup_pending', 'cleanup_abandoned')"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "cannot migrate image job recovery scope while cleanup_pending jobs exist"
        )


def _drop_job_update_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_status_transition")
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_provenance_no_update")


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in inspect(op.get_bind()).get_columns("image_output_jobs")
    }


def _create_provenance_trigger(*, scoped: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_provenance_no_update")
    scope_guards = (
        "OR NEW.artifact_store_identity IS NOT OLD.artifact_store_identity "
        "OR NEW.artifact_store_namespace IS NOT OLD.artifact_store_namespace "
        "OR NEW.expected_artifact_sequences IS NOT OLD.expected_artifact_sequences "
        if scoped
        else ""
    )
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_provenance_no_update "
        "BEFORE UPDATE ON image_output_jobs "
        "WHEN NEW.id IS NOT OLD.id "
        "OR NEW.customer_id IS NOT OLD.customer_id "
        "OR NEW.output_type IS NOT OLD.output_type "
        "OR NEW.style_card_version_id IS NOT OLD.style_card_version_id "
        "OR NEW.scene_style_id IS NOT OLD.scene_style_id "
        "OR NEW.output_ratio IS NOT OLD.output_ratio "
        "OR NEW.adapter_version IS NOT OLD.adapter_version "
        "OR NEW.request_metadata IS NOT OLD.request_metadata "
        "OR NEW.idempotency_key IS NOT OLD.idempotency_key "
        "OR NEW.created_at IS NOT OLD.created_at "
        + scope_guards
        + "BEGIN SELECT RAISE(ABORT, "
        "'image_output_job_provenance_immutable'); END"
    )


def _create_status_trigger(*, cleanup_abandoned: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_status_transition")
    abandoned_rule = ", 'cleanup_abandoned'" if cleanup_abandoned else ""
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_status_transition "
        "BEFORE UPDATE OF status ON image_output_jobs "
        "WHEN NEW.status IS NOT OLD.status AND NOT ("
        "(OLD.status = 'queued' AND NEW.status IN "
        "('generating', 'insufficient_information', 'permission_unknown', 'disabled')) "
        "OR (OLD.status = 'generating' AND NEW.status IN "
        "('machine_check', 'generation_failed', 'cleanup_pending'"
        + abandoned_rule
        + ", 'disabled')) "
        "OR (OLD.status = 'cleanup_pending' AND NEW.status IN "
        "('generation_failed'" + abandoned_rule + ")) "
        "OR (OLD.status = 'machine_check' AND NEW.status IN "
        "('awaiting_customer_review', 'authenticity_failed', 'disabled')) "
        "OR (OLD.status = 'awaiting_customer_review' AND NEW.status IN "
        "('customer_approved', 'customer_rejected', 'disabled'))"
        ") BEGIN SELECT RAISE(ABORT, "
        "'invalid_image_output_job_status_transition'); END"
    )


def upgrade() -> None:
    """Add immutable store scope plus a mutable worker claim token."""
    _assert_no_unscoped_cleanup()
    _drop_job_update_triggers()
    existing = _column_names()
    columns = (
        sa.Column("generation_claim_token", sa.String(length=36), nullable=True),
        sa.Column("artifact_store_identity", sa.String(length=100), nullable=True),
        sa.Column("artifact_store_namespace", sa.String(length=64), nullable=True),
        sa.Column("expected_artifact_sequences", sa.JSON(), nullable=True),
    )
    for column in columns:
        if column.name not in existing:
            op.add_column("image_output_jobs", column)
    _create_provenance_trigger(scoped=True)
    _create_status_trigger(cleanup_abandoned=True)


def downgrade() -> None:
    """Refuse to discard cleanup scope while unresolved artifacts may remain."""
    _assert_no_unscoped_cleanup()
    _drop_job_update_triggers()
    existing = _column_names()
    for column_name in (
        "expected_artifact_sequences",
        "artifact_store_namespace",
        "artifact_store_identity",
        "generation_claim_token",
    ):
        if column_name in existing:
            op.drop_column("image_output_jobs", column_name)
            existing.remove(column_name)
    _create_provenance_trigger(scoped=False)
    _create_status_trigger(cleanup_abandoned=False)
