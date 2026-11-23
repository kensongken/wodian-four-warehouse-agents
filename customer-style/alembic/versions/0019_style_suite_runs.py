"""Seal costed historical suite matrices independently of their execution state."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_style_suite_runs"
down_revision: str | None = "0018_style_template_slots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "style_suite_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False
        ),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("source_asset_id", sa.String(36), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("style_count", sa.Integer(), nullable=False),
        sa.Column("slot_count", sa.Integer(), nullable=False),
        sa.Column("child_job_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("candidate_estimate_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("maximum_estimated_cost_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("cost_basis", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("real_generation_enabled", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_style_suite_runs_customer_id"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "source_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_style_suite_runs_customer_source_product",
        ),
        sa.CheckConstraint(
            "style_count > 0 AND slot_count >= style_count AND child_job_count = slot_count AND candidate_count = child_job_count * 3",
            name="ck_style_suite_runs_counts",
        ),
        sa.CheckConstraint(
            "candidate_estimate_usd >= 0 AND maximum_estimated_cost_usd >= 0",
            name="ck_style_suite_runs_cost",
        ),
        sa.CheckConstraint(
            "NOT real_generation_enabled OR candidate_estimate_usd > 0",
            name="ck_style_suite_runs_real_cost",
        ),
        sa.CheckConstraint(
            "currency = 'USD' AND cost_basis = 'local_planning_estimate'",
            name="ck_style_suite_runs_cost_basis",
        ),
    )
    op.create_table(
        "style_suite_run_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False
        ),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("scene_style_id", sa.String(36), nullable=False),
        sa.Column("scene_style_version_id", sa.String(36), nullable=False),
        sa.Column("style_name", sa.String(255), nullable=False),
        sa.Column("style_version", sa.Integer(), nullable=False),
        sa.Column("slot_id", sa.String(36), nullable=False),
        sa.Column("slot_key", sa.String(64), nullable=False),
        sa.Column("style_snapshot", sa.JSON(), nullable=False),
        sa.Column("slot_snapshot", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id", "run_id"],
            ["style_suite_runs.customer_id", "style_suite_runs.id"],
            name="fk_style_suite_items_customer_run",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "scene_style_id"],
            ["scene_styles.customer_id", "scene_styles.id"],
            name="fk_style_suite_items_customer_scene",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "scene_style_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_suite_items_customer_version",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "slot_id"],
            ["style_template_slots.customer_id", "style_template_slots.id"],
            name="fk_style_suite_items_customer_slot",
        ),
        sa.UniqueConstraint(
            "run_id",
            "scene_style_version_id",
            "slot_key",
            name="uq_style_suite_run_items_version_slot",
        ),
    )
    for table, columns in {
        "style_suite_runs": ("customer_id",),
        "style_suite_run_items": ("customer_id", "run_id"),
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
        immutable = [
            column["name"]
            for column in sa.inspect(op.get_bind()).get_columns(table)
            if column["name"] != "state"
        ]
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE ON {table} WHEN "
            + " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in immutable)
            + f" BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END"
        )
    op.execute(
        "CREATE TRIGGER trg_style_suite_runs_source BEFORE INSERT ON style_suite_runs "
        "WHEN NOT EXISTS (SELECT 1 FROM product_assets a WHERE a.id = NEW.source_asset_id "
        "AND a.customer_id = NEW.customer_id AND a.product_id = NEW.product_id "
        "AND a.content_sha256 = NEW.source_content_sha256 AND a.confirmed = 1 "
        "AND a.identity = 'original_real_photo') "
        "OR abs(NEW.maximum_estimated_cost_usd - NEW.candidate_estimate_usd * NEW.candidate_count) > 0.0000005 "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_run_facts_mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_items_limit BEFORE INSERT ON style_suite_run_items "
        "WHEN (SELECT count(*) FROM style_suite_run_items WHERE run_id = NEW.run_id) "
        ">= (SELECT slot_count FROM style_suite_runs WHERE id = NEW.run_id) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_run_item_limit'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_items_snapshot BEFORE INSERT ON style_suite_run_items "
        "WHEN NOT EXISTS (SELECT 1 FROM style_template_slots s "
        "JOIN style_card_versions v ON v.id = s.style_card_version_id "
        "JOIN style_suite_runs r ON r.id = NEW.run_id AND r.customer_id = NEW.customer_id, "
        "json_each(v.content, '$.slots') slot "
        "WHERE s.id = NEW.slot_id AND s.customer_id = NEW.customer_id "
        "AND s.scene_style_id = NEW.scene_style_id AND s.style_card_version_id = NEW.scene_style_version_id "
        "AND s.slot_key = NEW.slot_key AND s.active = 1 "
        "AND v.customer_id = NEW.customer_id AND v.version = NEW.style_version "
        "AND json_extract(v.content, '$.name') = NEW.style_name "
        "AND json_extract(v.content, '$.historical_style') = 1 "
        "AND json_extract(v.content, '$.stability') = 'customer_confirmed' "
        "AND json(v.content) = json(NEW.style_snapshot) "
        "AND json_extract(slot.value, '$.id') = NEW.slot_id "
        "AND json(slot.value) = json(NEW.slot_snapshot)) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_run_item_snapshot_mismatch'); END"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM style_suite_runs"))
        .scalar_one()
    ):
        raise RuntimeError("cannot discard sealed historical style suite runs")
    op.drop_table("style_suite_run_items")
    op.drop_table("style_suite_runs")
