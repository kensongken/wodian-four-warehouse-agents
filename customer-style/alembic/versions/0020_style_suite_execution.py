"""Add parent idempotency and durable child execution links."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_style_suite_execution"
down_revision: str | None = "0019_style_suite_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_item_job_trigger(operation: str, suffix: str) -> None:
    rebinding = (
        "(OLD.output_job_id IS NOT NULL AND NEW.output_job_id IS NOT OLD.output_job_id) OR "
        if suffix == "update"
        else ""
    )
    op.execute(
        f"CREATE TRIGGER trg_style_suite_items_job_{suffix} BEFORE {operation} "
        "ON style_suite_run_items WHEN "
        + rebinding
        + "(NEW.output_job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM style_suite_runs r "
        "JOIN image_output_jobs j ON j.id = NEW.output_job_id AND j.customer_id = NEW.customer_id "
        "JOIN product_asset_links l ON l.output_job_id = j.id AND l.customer_id = NEW.customer_id "
        "JOIN product_assets a ON a.id = l.product_asset_id AND a.customer_id = NEW.customer_id "
        "WHERE r.id = NEW.run_id AND r.customer_id = NEW.customer_id "
        "AND j.output_type = 'product_style' "
        "AND j.style_card_version_id = NEW.scene_style_version_id "
        "AND j.scene_style_id = NEW.scene_style_id "
        "AND j.idempotency_key = 'suite:' || NEW.run_id || ':' || NEW.scene_style_version_id || ':' || NEW.slot_key || ':' || r.source_content_sha256 "
        "AND a.id = r.source_asset_id AND a.product_id = r.product_id "
        "AND a.content_sha256 = r.source_content_sha256 "
        "AND json_extract(j.request_metadata, '$.slot_key') = NEW.slot_key "
        "AND json_array_length(j.request_metadata, '$.sources') = 1 "
        "AND json_extract(j.request_metadata, '$.sources[0].asset_id') = r.source_asset_id "
        "AND json_extract(j.request_metadata, '$.sources[0].product_id') = r.product_id "
        "AND (SELECT count(*) FROM product_asset_links x WHERE x.output_job_id = j.id) = 1)) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_item_job_mismatch'); END"
    )


def upgrade() -> None:
    op.add_column(
        "style_suite_runs",
        sa.Column("idempotency_key", sa.String(255), nullable=False, server_default=""),
    )
    op.execute(
        "UPDATE style_suite_runs SET idempotency_key = 'legacy:' || id "
        "WHERE idempotency_key = ''"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_runs_key_nonblank BEFORE INSERT ON style_suite_runs "
        "WHEN trim(NEW.idempotency_key) = '' "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_run_key_blank'); END"
    )
    op.create_index(
        "uq_style_suite_runs_customer_key",
        "style_suite_runs",
        ["customer_id", "idempotency_key"],
        unique=True,
    )
    op.add_column(
        "style_suite_run_items",
        sa.Column("output_job_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "style_suite_run_items",
        sa.Column("failure_reason", sa.String(255), nullable=True),
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_runs_key_immutable BEFORE UPDATE ON style_suite_runs "
        "WHEN NEW.idempotency_key IS NOT OLD.idempotency_key "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_runs_immutable'); END"
    )
    _create_item_job_trigger("INSERT", "insert")
    _create_item_job_trigger("UPDATE OF output_job_id", "update")
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_status_transition")
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_status_transition "
        "BEFORE UPDATE OF status ON image_output_jobs "
        "WHEN NEW.status IS NOT OLD.status AND NOT ("
        "(OLD.status = 'queued' AND NEW.status IN "
        "('generating', 'insufficient_information', 'permission_unknown', 'disabled')) "
        "OR (OLD.status = 'generating' AND NEW.status IN "
        "('machine_check', 'generation_failed', 'cleanup_pending', 'cleanup_abandoned', 'disabled')) "
        "OR (OLD.status = 'cleanup_pending' AND NEW.status IN "
        "('generation_failed', 'cleanup_abandoned')) "
        "OR (OLD.status = 'generation_failed' AND NEW.status = 'generating') "
        "OR (OLD.status = 'machine_check' AND NEW.status IN "
        "('awaiting_customer_review', 'authenticity_failed', 'disabled')) "
        "OR (OLD.status = 'awaiting_customer_review' AND NEW.status IN "
        "('customer_approved', 'customer_rejected', 'disabled'))) "
        "BEGIN SELECT RAISE(ABORT, 'invalid_image_output_job_status_transition'); END"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM style_suite_runs"))
        .scalar_one()
    ):
        raise RuntimeError("cannot discard sealed historical style suite runs")
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_runs_key_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_runs_key_nonblank")
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_items_job_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_items_job_update")
    op.drop_index("uq_style_suite_runs_customer_key", table_name="style_suite_runs")
    op.drop_column("style_suite_run_items", "failure_reason")
    op.drop_column("style_suite_run_items", "output_job_id")
    op.drop_column("style_suite_runs", "idempotency_key")
    op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_status_transition")
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_status_transition "
        "BEFORE UPDATE OF status ON image_output_jobs "
        "WHEN NEW.status IS NOT OLD.status AND NOT ("
        "(OLD.status = 'queued' AND NEW.status IN "
        "('generating', 'insufficient_information', 'permission_unknown', 'disabled')) "
        "OR (OLD.status = 'generating' AND NEW.status IN "
        "('machine_check', 'generation_failed', 'cleanup_pending', 'cleanup_abandoned', 'disabled')) "
        "OR (OLD.status = 'cleanup_pending' AND NEW.status IN "
        "('generation_failed', 'cleanup_abandoned')) "
        "OR (OLD.status = 'machine_check' AND NEW.status IN "
        "('awaiting_customer_review', 'authenticity_failed', 'disabled')) "
        "OR (OLD.status = 'awaiting_customer_review' AND NEW.status IN "
        "('customer_approved', 'customer_rejected', 'disabled'))) "
        "BEGIN SELECT RAISE(ABORT, 'invalid_image_output_job_status_transition'); END"
    )
