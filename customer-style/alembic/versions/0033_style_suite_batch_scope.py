"""Persist signed batch authorization seals on suite runs and items."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_style_suite_batch_scope"
down_revision: str | None = "0032_style_reference_packs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_item_job_trigger(operation: str, suffix: str, *, sealed_pack: bool) -> None:
    rebinding = (
        "(OLD.output_job_id IS NOT NULL AND NEW.output_job_id IS NOT OLD.output_job_id) OR "
        if suffix == "update"
        else ""
    )
    key_suffix = (
        " || CASE WHEN NEW.style_reference_manifest_sha256 IS NOT NULL "
        "THEN ':' || NEW.style_reference_manifest_sha256 ELSE '' END"
        if sealed_pack
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
        "AND j.idempotency_key = 'suite:' || NEW.run_id || ':' || "
        "NEW.scene_style_version_id || ':' || NEW.slot_key || ':' || "
        f"r.source_content_sha256{key_suffix} "
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
        "style_suite_runs", sa.Column("preview_payload_sha256", sa.String(64))
    )
    op.add_column(
        "style_suite_runs", sa.Column("confirmed_maximum_usd", sa.Numeric(18, 6))
    )
    op.add_column(
        "style_suite_runs", sa.Column("candidate_count_per_item", sa.Integer())
    )
    op.add_column(
        "style_suite_runs", sa.Column("authorization_manifest_sha256", sa.String(64))
    )
    op.add_column(
        "style_suite_run_items", sa.Column("style_reference_pack_id", sa.String(36))
    )
    op.add_column(
        "style_suite_run_items",
        sa.Column("style_reference_manifest_sha256", sa.String(64)),
    )
    op.add_column(
        "style_suite_run_items", sa.Column("item_authorized_usd", sa.Numeric(18, 6))
    )
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_runs_source")
    op.execute(
        "CREATE TRIGGER trg_style_suite_runs_source BEFORE INSERT ON style_suite_runs "
        "WHEN NOT EXISTS (SELECT 1 FROM product_assets a WHERE a.id = NEW.source_asset_id "
        "AND a.customer_id = NEW.customer_id AND a.product_id = NEW.product_id "
        "AND a.content_sha256 = NEW.source_content_sha256 AND a.confirmed = 1 "
        "AND a.identity = 'original_real_photo') "
        "OR abs(NEW.maximum_estimated_cost_usd - NEW.candidate_estimate_usd * "
        "CASE WHEN NEW.candidate_count_per_item IS NOT NULL "
        "THEN NEW.child_job_count * NEW.candidate_count_per_item "
        "ELSE NEW.candidate_count END) > 0.0000005 "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_run_facts_mismatch'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_items_snapshot")
    op.execute(
        "CREATE TRIGGER trg_style_suite_items_snapshot BEFORE INSERT ON style_suite_run_items "
        "WHEN NOT EXISTS (SELECT 1 FROM style_template_slots s "
        "JOIN style_card_versions v ON v.id = s.style_card_version_id "
        "JOIN style_suite_runs r ON r.id = NEW.run_id AND r.customer_id = NEW.customer_id, "
        "json_each(v.content, '$.slots') slot "
        "WHERE s.id = NEW.slot_id AND s.customer_id = NEW.customer_id "
        "AND s.scene_style_id = NEW.scene_style_id "
        "AND s.style_card_version_id = NEW.scene_style_version_id "
        "AND s.slot_key = NEW.slot_key AND s.active = 1 "
        "AND v.customer_id = NEW.customer_id AND v.version = NEW.style_version "
        "AND json_extract(v.content, '$.name') = NEW.style_name "
        "AND json_extract(v.content, '$.historical_style') = 1 "
        "AND json_extract(v.content, '$.stability') = 'customer_confirmed' "
        "AND NOT EXISTS (SELECT fullkey, type, atom FROM json_tree(v.content) "
        "EXCEPT SELECT fullkey, type, atom FROM json_tree(NEW.style_snapshot)) "
        "AND NOT EXISTS (SELECT fullkey, type, atom FROM json_tree(NEW.style_snapshot) "
        "EXCEPT SELECT fullkey, type, atom FROM json_tree(v.content)) "
        "AND json_extract(slot.value, '$.id') = NEW.slot_id "
        "AND NOT EXISTS (SELECT fullkey, type, atom FROM json_tree(slot.value) "
        "EXCEPT SELECT fullkey, type, atom FROM json_tree(NEW.slot_snapshot)) "
        "AND NOT EXISTS (SELECT fullkey, type, atom FROM json_tree(NEW.slot_snapshot) "
        "EXCEPT SELECT fullkey, type, atom FROM json_tree(slot.value))) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_run_item_snapshot_mismatch'); END"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_items_job_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_items_job_update")
    _create_item_job_trigger("INSERT", "insert", sealed_pack=True)
    _create_item_job_trigger("UPDATE OF output_job_id", "update", sealed_pack=True)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_items_job_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_items_job_update")
    _create_item_job_trigger("INSERT", "insert", sealed_pack=False)
    _create_item_job_trigger("UPDATE OF output_job_id", "update", sealed_pack=False)
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_items_snapshot")
    op.execute(
        "CREATE TRIGGER trg_style_suite_items_snapshot BEFORE INSERT ON style_suite_run_items "
        "WHEN NOT EXISTS (SELECT 1 FROM style_template_slots s "
        "JOIN style_card_versions v ON v.id = s.style_card_version_id "
        "JOIN style_suite_runs r ON r.id = NEW.run_id AND r.customer_id = NEW.customer_id, "
        "json_each(v.content, '$.slots') slot "
        "WHERE s.id = NEW.slot_id AND s.customer_id = NEW.customer_id "
        "AND s.scene_style_id = NEW.scene_style_id "
        "AND s.style_card_version_id = NEW.scene_style_version_id "
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
    op.execute("DROP TRIGGER IF EXISTS trg_style_suite_runs_source")
    op.execute(
        "CREATE TRIGGER trg_style_suite_runs_source BEFORE INSERT ON style_suite_runs "
        "WHEN NOT EXISTS (SELECT 1 FROM product_assets a WHERE a.id = NEW.source_asset_id "
        "AND a.customer_id = NEW.customer_id AND a.product_id = NEW.product_id "
        "AND a.content_sha256 = NEW.source_content_sha256 AND a.confirmed = 1 "
        "AND a.identity = 'original_real_photo') "
        "OR abs(NEW.maximum_estimated_cost_usd - NEW.candidate_estimate_usd * "
        "NEW.candidate_count) > 0.0000005 "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_run_facts_mismatch'); END"
    )
    op.drop_column("style_suite_run_items", "item_authorized_usd")
    op.drop_column("style_suite_run_items", "style_reference_manifest_sha256")
    op.drop_column("style_suite_run_items", "style_reference_pack_id")
    op.drop_column("style_suite_runs", "authorization_manifest_sha256")
    op.drop_column("style_suite_runs", "candidate_count_per_item")
    op.drop_column("style_suite_runs", "confirmed_maximum_usd")
    op.drop_column("style_suite_runs", "preview_payload_sha256")
