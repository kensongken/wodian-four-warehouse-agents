"""Add local execution fencing fields and opaque cleanup capabilities."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision: str = "0008_image_job_fencing_capability"
down_revision: str | None = "0007_image_job_recovery_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in inspect(op.get_bind()).get_columns("image_output_jobs")
    }


def _assert_no_unresolved_cleanup() -> None:
    if "artifact_store_capability" not in _column_names():
        # SQLite DDL is non-transactional. A retry that sees this column gone has
        # already passed the guard in an earlier downgrade attempt, so re-running
        # an unscoped status check would misclassify legal NULL-capability rows.
        return
    count = (
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM image_output_jobs "
                "WHERE artifact_store_capability IS NOT NULL AND "
                "status NOT IN ('generation_failed', 'authenticity_failed', "
                "'customer_approved', 'customer_rejected', 'disabled')"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "cannot discard image job recovery capability while unfinished jobs exist"
        )


def _assert_no_older_executor_jobs() -> None:
    count = (
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM image_output_jobs "
                "WHERE status IN ('queued', 'generating', 'cleanup_pending')"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "cannot upgrade 0008 while queued, generating, or cleanup_pending jobs "
            "belong to an older local executor"
        )


def _replace_provenance_trigger(*, capability_scoped: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_provenance_no_update")
    capability_guard = (
        "OR NEW.artifact_store_capability IS NOT OLD.artifact_store_capability "
        if capability_scoped
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
        "OR NEW.artifact_store_identity IS NOT OLD.artifact_store_identity "
        "OR NEW.artifact_store_namespace IS NOT OLD.artifact_store_namespace "
        "OR NEW.expected_artifact_sequences IS NOT OLD.expected_artifact_sequences "
        + capability_guard
        + "BEGIN SELECT RAISE(ABORT, "
        "'image_output_job_provenance_immutable'); END"
    )


def upgrade() -> None:
    """Guard legacy work and bind cleanup to store-private process-local proof."""
    _assert_no_older_executor_jobs()
    existing = _column_names()
    columns = (
        sa.Column("generation_lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("artifact_store_capability", sa.String(length=128)),
    )
    for column in columns:
        if column.name not in existing:
            op.add_column("image_output_jobs", column)
    _replace_provenance_trigger(capability_scoped=True)


def downgrade() -> None:
    """Never discard cleanup proof while residual-artifact work is unresolved."""
    _assert_no_unresolved_cleanup()
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_provenance_no_update")
    existing = _column_names()
    for column_name in (
        "artifact_store_capability",
        "generation_lease_expires_at",
    ):
        if column_name in existing:
            op.drop_column("image_output_jobs", column_name)
            existing.remove(column_name)
    _replace_provenance_trigger(capability_scoped=False)
