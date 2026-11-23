"""Persist path-free foreground pipeline evidence for restart recovery."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042_foreground_pipeline_evidence"
down_revision: str | None = "0041_observation_json_nulls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_foreground_update_trigger(*, include_pipeline: bool) -> None:
    immutable_columns = [
        "id",
        "customer_id",
        "product_asset_id",
        "product_id",
        "version",
        "source_content_sha256",
        "foreground_sha256",
        "mask_sha256",
        "width",
        "height",
        "extractor_version",
        "manifest_sha256",
    ]
    if include_pipeline:
        immutable_columns.extend(
            ["pipeline_manifest_sha256", "stage_receipts_sha256"]
        )
    immutable_columns.extend(["quality_flags", "created_at"])
    foreground_same = " AND ".join(
        f"NEW.{column} IS OLD.{column}" for column in immutable_columns
    )
    op.execute(
        "CREATE TRIGGER trg_product_foreground_update "
        "BEFORE UPDATE ON product_foreground_versions WHEN NOT ("
        "OLD.state = 'proposed' AND NEW.state IN ('confirmed', 'rejected') AND "
        f"{foreground_same} AND length(trim(NEW.confirmation_key)) > 0 "
        "AND NEW.decided_at IS NOT NULL) "
        "BEGIN SELECT RAISE(ABORT, 'product_foreground_immutable'); END"
    )


def upgrade() -> None:
    for table, columns in (
        (
            "product_foreground_versions",
            ("pipeline_manifest_sha256", "stage_receipts_sha256"),
        ),
        (
            "foreground_shadow_decisions",
            (
                "pipeline_manifest_sha256",
                "stage_receipts_sha256",
                "risk_parameters_sha256",
            ),
        ),
    ):
        for column in columns:
            op.add_column(table, sa.Column(column, sa.String(64), nullable=True))

    op.execute("DROP TRIGGER trg_product_foreground_update")
    _install_foreground_update_trigger(include_pipeline=True)
    op.execute(
        "CREATE TRIGGER trg_product_foreground_pipeline_evidence_insert "
        "BEFORE INSERT ON product_foreground_versions WHEN NOT ("
        "(NEW.pipeline_manifest_sha256 IS NULL AND NEW.stage_receipts_sha256 IS NULL) OR "
        "(length(NEW.pipeline_manifest_sha256) = 64 AND "
        "length(NEW.stage_receipts_sha256) = 64)) "
        "BEGIN SELECT RAISE(ABORT, 'foreground_pipeline_evidence_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_foreground_shadow_pipeline_evidence_insert "
        "BEFORE INSERT ON foreground_shadow_decisions WHEN NOT ("
        "(NEW.pipeline_manifest_sha256 IS NULL AND NEW.stage_receipts_sha256 IS NULL "
        "AND NEW.risk_parameters_sha256 IS NULL) OR "
        "(length(NEW.pipeline_manifest_sha256) = 64 AND "
        "length(NEW.stage_receipts_sha256) = 64 AND "
        "length(NEW.risk_parameters_sha256) = 64)) "
        "BEGIN SELECT RAISE(ABORT, 'foreground_pipeline_evidence_invalid'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    pipeline_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM product_foreground_versions "
            "WHERE pipeline_manifest_sha256 IS NOT NULL OR stage_receipts_sha256 IS NOT NULL"
        )
    ).scalar_one()
    shadow_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM foreground_shadow_decisions "
            "WHERE pipeline_manifest_sha256 IS NOT NULL "
            "OR stage_receipts_sha256 IS NOT NULL OR risk_parameters_sha256 IS NOT NULL"
        )
    ).scalar_one()
    if pipeline_count or shadow_count:
        raise RuntimeError("cannot discard foreground pipeline evidence")
    op.execute("DROP TRIGGER trg_foreground_shadow_pipeline_evidence_insert")
    op.execute("DROP TRIGGER trg_product_foreground_pipeline_evidence_insert")
    op.execute("DROP TRIGGER trg_product_foreground_update")
    for table, columns in (
        (
            "foreground_shadow_decisions",
            (
                "risk_parameters_sha256",
                "stage_receipts_sha256",
                "pipeline_manifest_sha256",
            ),
        ),
        (
            "product_foreground_versions",
            ("stage_receipts_sha256", "pipeline_manifest_sha256"),
        ),
    ):
        for column in columns:
            op.drop_column(table, column)
    _install_foreground_update_trigger(include_pipeline=False)
