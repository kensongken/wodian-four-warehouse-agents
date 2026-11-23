"""Persist immutable evidence for pixel-preserving isolated compositions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_pixel_preserving_isolated_style"
down_revision: str | None = "0034_real_fidelity_suite_single_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_COUNT_CHECK = (
    "style_count > 0 AND slot_count >= style_count AND child_job_count = slot_count "
    "AND candidate_count = child_job_count * COALESCE(candidate_count_per_item, 3)"
)
_LEGACY_COUNT_CHECK = (
    "style_count > 0 AND slot_count >= style_count AND child_job_count = slot_count "
    "AND candidate_count = child_job_count * 3"
)


def _sqlite_triggers_referencing(table_name: str) -> list[tuple[str, str]]:
    if op.get_bind().dialect.name != "sqlite":
        return []
    rows = op.get_bind().execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND lower(sql) LIKE :table_name AND sql IS NOT NULL"
        ),
        {"table_name": f"%{table_name.lower()}%"},
    )
    return [(str(row[0]), str(row[1])) for row in rows]


def _replace_style_suite_count_check(sqltext: str) -> None:
    triggers = _sqlite_triggers_referencing("style_suite_runs")
    if op.get_bind().dialect.name == "sqlite":
        connection = op.get_bind()
        existing_fk_violations = {
            tuple(row)
            for row in connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        }
        with op.get_context().autocommit_block():
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                for name, _ in triggers:
                    op.execute(f'DROP TRIGGER IF EXISTS "{name}"')
                with op.batch_alter_table(
                    "style_suite_runs", recreate="always"
                ) as batch:
                    batch.drop_constraint("ck_style_suite_runs_counts", type_="check")
                    batch.create_check_constraint("ck_style_suite_runs_counts", sqltext)
                for _, statement in triggers:
                    op.execute(statement)
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            remaining_fk_violations = {
                tuple(row)
                for row in connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            }
            if remaining_fk_violations != existing_fk_violations:
                raise RuntimeError(
                    "style suite count migration foreign key check failed"
                )
        return

    op.drop_constraint("ck_style_suite_runs_counts", "style_suite_runs", type_="check")
    op.create_check_constraint(
        "ck_style_suite_runs_counts", "style_suite_runs", sqltext
    )


def upgrade() -> None:
    # Migration 0033 introduced candidate_count_per_item with historical rows
    # still carrying the legacy three-candidate total. Preserve that exact
    # meaning before the new check allows one-candidate pixel cells.
    op.execute(
        "UPDATE style_suite_runs SET candidate_count_per_item = 3 "
        "WHERE candidate_count = child_job_count * 3 "
        "AND COALESCE(candidate_count_per_item, 1) = 1"
    )
    _replace_style_suite_count_check(_CURRENT_COUNT_CHECK)
    op.create_table(
        "product_foreground_versions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("foreground_sha256", sa.String(64), nullable=False),
        sa.Column("mask_sha256", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("extractor_version", sa.String(100), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("quality_flags", sa.JSON(), nullable=False),
        sa.Column("confirmation_key", sa.String(255)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "product_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_product_foregrounds_customer_asset_product",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_product_foregrounds_customer_id"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "product_asset_id",
            "version",
            name="uq_product_foregrounds_customer_asset_version",
        ),
        sa.CheckConstraint("version > 0", name="ck_product_foregrounds_version"),
        sa.CheckConstraint(
            "width > 0 AND height > 0", name="ck_product_foregrounds_size"
        ),
        sa.CheckConstraint(
            "length(source_content_sha256) = 64 "
            "AND length(foreground_sha256) = 64 "
            "AND length(mask_sha256) = 64 "
            "AND length(manifest_sha256) = 64",
            name="ck_product_foregrounds_digests",
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'confirmed', 'rejected')",
            name="ck_product_foregrounds_state",
        ),
        sa.CheckConstraint(
            "(state = 'proposed' AND confirmation_key IS NULL AND decided_at IS NULL) OR "
            "(state IN ('confirmed', 'rejected') AND "
            "length(trim(confirmation_key)) > 0 AND decided_at IS NOT NULL)",
            name="ck_product_foregrounds_decision",
        ),
    )
    op.create_index(
        "ix_product_foregrounds_customer_asset",
        "product_foreground_versions",
        ["customer_id", "product_asset_id"],
    )

    op.create_table(
        "pixel_background_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("style_suite_run_id", sa.String(36), nullable=False),
        sa.Column("foreground_version_id", sa.String(36), nullable=False),
        sa.Column("authorized_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("authorized_manifest", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("maximum_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("dispatch_state", sa.String(32), nullable=False),
        sa.Column("outbound_attempts", sa.Integer(), nullable=False),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("review_state", sa.String(32), nullable=False),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_suite_run_id"],
            ["style_suite_runs.customer_id", "style_suite_runs.id"],
            name="fk_pixel_background_customer_suite",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "foreground_version_id"],
            [
                "product_foreground_versions.customer_id",
                "product_foreground_versions.id",
            ],
            name="fk_pixel_background_customer_foreground",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_pixel_background_customer_id"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_pixel_background_customer_key",
        ),
        sa.CheckConstraint("maximum_usd > 0", name="ck_pixel_background_usd"),
        sa.CheckConstraint(
            "length(authorized_manifest_sha256) = 64",
            name="ck_pixel_background_manifest_digest",
        ),
        sa.CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64",
            name="ck_pixel_background_result_digest",
        ),
        sa.CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_pixel_background_dispatch_state",
        ),
        sa.CheckConstraint(
            "outbound_attempts IN (0, 1)", name="ck_pixel_background_one_attempt"
        ),
        sa.CheckConstraint(
            "review_state IN ('awaiting_review', 'confirmed', 'rejected')",
            name="ck_pixel_background_review_state",
        ),
        sa.CheckConstraint(
            "(review_state = 'awaiting_review' AND reviewed_at IS NULL) OR "
            "(review_state IN ('confirmed', 'rejected') AND "
            "reviewed_at IS NOT NULL AND result_sha256 IS NOT NULL)",
            name="ck_pixel_background_review_decision",
        ),
    )
    op.create_index(
        "ix_pixel_background_customer_suite",
        "pixel_background_runs",
        ["customer_id", "style_suite_run_id"],
    )

    op.create_table(
        "pixel_composition_receipts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("foreground_version_id", sa.String(36), nullable=False),
        sa.Column("background_run_id", sa.String(36), nullable=False),
        sa.Column("image_candidate_id", sa.String(36), nullable=False),
        sa.Column("transform_payload", sa.JSON(), nullable=False),
        sa.Column("compositor_version", sa.String(100), nullable=False),
        sa.Column("verification_state", sa.String(16), nullable=False),
        sa.Column("foreground_sha256", sa.String(64), nullable=False),
        sa.Column("background_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("transformed_alpha_sha256", sa.String(64), nullable=False),
        sa.Column("foreground_rgb_sha256", sa.String(64), nullable=False),
        sa.Column("publishable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "foreground_version_id"],
            [
                "product_foreground_versions.customer_id",
                "product_foreground_versions.id",
            ],
            name="fk_pixel_receipt_customer_foreground",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "background_run_id"],
            ["pixel_background_runs.customer_id", "pixel_background_runs.id"],
            name="fk_pixel_receipt_customer_background",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "image_candidate_id"],
            ["image_candidates.customer_id", "image_candidates.id"],
            name="fk_pixel_receipt_customer_candidate",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "id", name="uq_pixel_receipts_customer_id"),
        sa.UniqueConstraint("image_candidate_id", name="uq_pixel_receipts_candidate"),
        sa.CheckConstraint(
            "verification_state = 'verified'",
            name="ck_pixel_receipts_verification_state",
        ),
        sa.CheckConstraint("publishable = 0", name="ck_pixel_receipts_not_publishable"),
        sa.CheckConstraint(
            "length(foreground_sha256) = 64 "
            "AND length(background_sha256) = 64 "
            "AND length(result_sha256) = 64 "
            "AND length(transformed_alpha_sha256) = 64 "
            "AND length(foreground_rgb_sha256) = 64",
            name="ck_pixel_receipts_digests",
        ),
    )
    op.create_index(
        "ix_pixel_receipts_customer_foreground",
        "pixel_composition_receipts",
        ["customer_id", "foreground_version_id"],
    )
    _install_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in (
        "pixel_composition_receipts",
        "pixel_background_runs",
        "product_foreground_versions",
    ):
        if bind.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one():
            raise RuntimeError("cannot discard pixel preservation evidence")

    for trigger_name in (
        "trg_pixel_composition_receipt_no_delete",
        "trg_pixel_composition_receipt_immutable",
        "trg_pixel_composition_receipt_facts",
        "trg_pixel_background_no_delete",
        "trg_pixel_background_update",
        "trg_product_foreground_no_delete",
        "trg_product_foreground_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    op.drop_index(
        "ix_pixel_receipts_customer_foreground",
        table_name="pixel_composition_receipts",
    )
    op.drop_table("pixel_composition_receipts")
    op.drop_index(
        "ix_pixel_background_customer_suite", table_name="pixel_background_runs"
    )
    op.drop_table("pixel_background_runs")
    op.drop_index(
        "ix_product_foregrounds_customer_asset",
        table_name="product_foreground_versions",
    )
    op.drop_table("product_foreground_versions")
    _replace_style_suite_count_check(_LEGACY_COUNT_CHECK)


def _install_triggers() -> None:
    immutable_foreground_columns = (
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
        "quality_flags",
        "created_at",
    )
    foreground_same = " AND ".join(
        f"NEW.{column} IS OLD.{column}" for column in immutable_foreground_columns
    )
    op.execute(
        "CREATE TRIGGER trg_product_foreground_update "
        "BEFORE UPDATE ON product_foreground_versions WHEN NOT ("
        "OLD.state = 'proposed' AND NEW.state IN ('confirmed', 'rejected') AND "
        f"{foreground_same} AND length(trim(NEW.confirmation_key)) > 0 "
        "AND NEW.decided_at IS NOT NULL) "
        "BEGIN SELECT RAISE(ABORT, 'product_foreground_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_foreground_no_delete "
        "BEFORE DELETE ON product_foreground_versions "
        "BEGIN SELECT RAISE(ABORT, 'product_foreground_immutable'); END"
    )

    immutable_background_columns = (
        "id",
        "customer_id",
        "style_suite_run_id",
        "foreground_version_id",
        "authorized_manifest_sha256",
        "authorized_manifest",
        "idempotency_key",
        "model",
        "maximum_usd",
        "created_at",
    )
    background_same = " AND ".join(
        f"NEW.{column} IS OLD.{column}" for column in immutable_background_columns
    )
    outcome_same = " AND ".join(
        f"NEW.{column} IS OLD.{column}"
        for column in (
            "dispatch_state",
            "outbound_attempts",
            "result_sha256",
            "failure_code",
        )
    )
    op.execute(
        "CREATE TRIGGER trg_pixel_background_update "
        "BEFORE UPDATE ON pixel_background_runs WHEN NOT ("
        f"{background_same} AND OLD.review_state = 'awaiting_review' AND ("
        "(NEW.review_state = 'awaiting_review' AND NEW.reviewed_at IS NULL) OR "
        f"(NEW.review_state IN ('confirmed', 'rejected') AND {outcome_same} "
        "AND NEW.reviewed_at IS NOT NULL))) "
        "BEGIN SELECT RAISE(ABORT, 'pixel_background_run_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_pixel_background_no_delete "
        "BEFORE DELETE ON pixel_background_runs "
        "BEGIN SELECT RAISE(ABORT, 'pixel_background_run_immutable'); END"
    )

    op.execute(
        "CREATE TRIGGER trg_pixel_composition_receipt_facts "
        "BEFORE INSERT ON pixel_composition_receipts WHEN NOT EXISTS ("
        "SELECT 1 FROM product_foreground_versions f "
        "JOIN pixel_background_runs b ON b.id = NEW.background_run_id "
        "AND b.customer_id = NEW.customer_id "
        "JOIN image_candidates c ON c.id = NEW.image_candidate_id "
        "AND c.customer_id = NEW.customer_id "
        "WHERE f.id = NEW.foreground_version_id AND f.customer_id = NEW.customer_id "
        "AND f.state = 'confirmed' AND f.foreground_sha256 = NEW.foreground_sha256 "
        "AND b.foreground_version_id = f.id AND b.review_state = 'confirmed' "
        "AND b.result_sha256 = NEW.background_sha256 "
        "AND c.content_sha256 = NEW.result_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'pixel_composition_receipt_mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_pixel_composition_receipt_immutable "
        "BEFORE UPDATE ON pixel_composition_receipts "
        "BEGIN SELECT RAISE(ABORT, 'pixel_composition_receipt_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_pixel_composition_receipt_no_delete "
        "BEFORE DELETE ON pixel_composition_receipts "
        "BEGIN SELECT RAISE(ABORT, 'pixel_composition_receipt_immutable'); END"
    )
