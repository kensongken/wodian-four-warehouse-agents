"""Persist append-only foreground benchmark and shadow evidence."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_foreground_benchmark_shadow_gate"
down_revision: str | None = "0036_product_foreground_mask_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "foreground_benchmark_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("dataset_seal_sha256", sa.String(64), nullable=False),
        sa.Column("model_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("total_samples", sa.Integer(), nullable=False),
        sa.Column("completed_samples", sa.Integer(), nullable=False),
        sa.Column("failed_samples", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "id", name="uq_foreground_benchmark_runs_customer_id"),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_foreground_benchmark_runs_customer_idempotency",
        ),
        sa.CheckConstraint(
            "length(dataset_seal_sha256) = 64 AND length(model_manifest_sha256) = 64 "
            "AND length(config_sha256) = 64",
            name="ck_foreground_benchmark_runs_digests",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_foreground_benchmark_runs_status",
        ),
        sa.CheckConstraint(
            "total_samples > 0 AND completed_samples >= 0 AND failed_samples >= 0 "
            "AND completed_samples + failed_samples <= total_samples",
            name="ck_foreground_benchmark_runs_counts",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL AND failure_code IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND failure_code IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND length(trim(failure_code)) > 0)",
            name="ck_foreground_benchmark_runs_terminal",
        ),
    )
    op.create_index(
        "ix_foreground_benchmark_runs_customer_created",
        "foreground_benchmark_runs",
        ["customer_id", "created_at"],
    )
    op.create_index(
        "ix_foreground_benchmark_runs_dataset",
        "foreground_benchmark_runs",
        ["dataset_seal_sha256"],
    )
    op.create_index(
        "ix_foreground_benchmark_runs_model",
        "foreground_benchmark_runs",
        ["model_manifest_sha256"],
    )
    op.create_table(
        "foreground_benchmark_sample_results",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("sample_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("true_positive_pixels", sa.Integer(), nullable=False),
        sa.Column("false_positive_pixels", sa.Integer(), nullable=False),
        sa.Column("false_negative_pixels", sa.Integer(), nullable=False),
        sa.Column("iou", sa.Numeric(12, 10), nullable=False),
        sa.Column("foreground_recall", sa.Numeric(12, 10), nullable=False),
        sa.Column("background_contamination", sa.Numeric(12, 10), nullable=False),
        sa.Column("critical_component_recalls", sa.JSON(), nullable=False),
        sa.Column("critical_component_preservation_rate", sa.Numeric(12, 10), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id", "run_id"],
            ["foreground_benchmark_runs.customer_id", "foreground_benchmark_runs.id"],
            name="fk_foreground_benchmark_results_customer_run",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sample_id", name="uq_foreground_benchmark_result_sample"),
        sa.UniqueConstraint("customer_id", "id", name="uq_foreground_benchmark_results_customer_id"),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_foreground_benchmark_results_status",
        ),
        sa.CheckConstraint(
            "verdict IN ('auto_eligible', 'human_review_required', 'blocked')",
            name="ck_foreground_benchmark_results_verdict",
        ),
        sa.CheckConstraint(
            "true_positive_pixels >= 0 AND false_positive_pixels >= 0 "
            "AND false_negative_pixels >= 0 AND elapsed_ms >= 0",
            name="ck_foreground_benchmark_results_counts",
        ),
    )
    op.create_index(
        "ix_foreground_benchmark_results_customer_run",
        "foreground_benchmark_sample_results",
        ["customer_id", "run_id"],
    )
    op.create_table(
        "foreground_shadow_decisions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36)),
        sa.Column("sample_result_id", sa.String(36)),
        sa.Column("foreground_version_id", sa.String(36)),
        sa.Column("model_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("evidence_complete", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id", "run_id"],
            ["foreground_benchmark_runs.customer_id", "foreground_benchmark_runs.id"],
            name="fk_foreground_shadow_customer_run",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "sample_result_id"],
            [
                "foreground_benchmark_sample_results.customer_id",
                "foreground_benchmark_sample_results.id",
            ],
            name="fk_foreground_shadow_customer_result",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "foreground_version_id"],
            ["product_foreground_versions.customer_id", "product_foreground_versions.id"],
            name="fk_foreground_shadow_customer_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sample_result_id", name="uq_foreground_shadow_result"),
        sa.UniqueConstraint("foreground_version_id", name="uq_foreground_shadow_version"),
        sa.CheckConstraint(
            "(run_id IS NOT NULL AND sample_result_id IS NOT NULL AND foreground_version_id IS NULL) OR "
            "(run_id IS NULL AND sample_result_id IS NULL AND foreground_version_id IS NOT NULL)",
            name="ck_foreground_shadow_context",
        ),
        sa.CheckConstraint(
            "length(model_manifest_sha256) = 64 AND length(config_sha256) = 64",
            name="ck_foreground_shadow_digests",
        ),
        sa.CheckConstraint(
            "verdict IN ('auto_eligible', 'human_review_required', 'blocked')",
            name="ck_foreground_shadow_verdict",
        ),
    )
    op.create_index(
        "ix_foreground_shadow_customer_run",
        "foreground_shadow_decisions",
        ["customer_id", "run_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_foreground_benchmark_run_identity_immutable "
        "BEFORE UPDATE ON foreground_benchmark_runs WHEN "
        "OLD.customer_id != NEW.customer_id OR OLD.dataset_seal_sha256 != NEW.dataset_seal_sha256 "
        "OR OLD.model_manifest_sha256 != NEW.model_manifest_sha256 "
        "OR OLD.config_sha256 != NEW.config_sha256 OR OLD.idempotency_key != NEW.idempotency_key "
        "OR OLD.total_samples != NEW.total_samples "
        "OR OLD.created_at != NEW.created_at "
        "BEGIN SELECT RAISE(ABORT, 'foreground_benchmark_evidence_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_foreground_benchmark_run_terminal_immutable "
        "BEFORE UPDATE ON foreground_benchmark_runs WHEN OLD.status != 'running' "
        "BEGIN SELECT RAISE(ABORT, 'foreground_benchmark_evidence_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_foreground_benchmark_run_no_delete BEFORE DELETE ON foreground_benchmark_runs "
        "BEGIN SELECT RAISE(ABORT, 'foreground_benchmark_evidence_immutable'); END"
    )
    for table, prefix in (
        ("foreground_benchmark_sample_results", "foreground_benchmark_result"),
        ("foreground_shadow_decisions", "foreground_shadow_decision"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{prefix}_immutable BEFORE UPDATE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'foreground_benchmark_evidence_immutable'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{prefix}_no_delete BEFORE DELETE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'foreground_benchmark_evidence_immutable'); END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "foreground_shadow_decisions",
        "foreground_benchmark_sample_results",
        "foreground_benchmark_runs",
    ):
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one():
            raise RuntimeError("cannot discard foreground benchmark evidence")
    for trigger in (
        "trg_foreground_shadow_decision_no_delete",
        "trg_foreground_shadow_decision_immutable",
        "trg_foreground_benchmark_result_no_delete",
        "trg_foreground_benchmark_result_immutable",
        "trg_foreground_benchmark_run_no_delete",
        "trg_foreground_benchmark_run_terminal_immutable",
        "trg_foreground_benchmark_run_identity_immutable",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index("ix_foreground_shadow_customer_run", table_name="foreground_shadow_decisions")
    op.drop_table("foreground_shadow_decisions")
    op.drop_index(
        "ix_foreground_benchmark_results_customer_run",
        table_name="foreground_benchmark_sample_results",
    )
    op.drop_table("foreground_benchmark_sample_results")
    op.drop_index("ix_foreground_benchmark_runs_model", table_name="foreground_benchmark_runs")
    op.drop_index("ix_foreground_benchmark_runs_dataset", table_name="foreground_benchmark_runs")
    op.drop_index(
        "ix_foreground_benchmark_runs_customer_created",
        table_name="foreground_benchmark_runs",
    )
    op.drop_table("foreground_benchmark_runs")
