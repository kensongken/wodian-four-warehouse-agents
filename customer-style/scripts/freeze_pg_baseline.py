"""Developer tool: materialize a REVIEWED fresh-database PG baseline once.

Do not rerun this tool to upgrade a deployed database. Append a migration.
"""

from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from opc_style.models import Base

root = Path(__file__).resolve().parents[1]
dialect = postgresql.dialect()
statements: list[str] = []
for table in Base.metadata.sorted_tables:
    statements.append(str(CreateTable(table).compile(dialect=dialect)).strip())
    for index in sorted(table.indexes, key=lambda value: str(value.name)):
        statements.append(str(CreateIndex(index).compile(dialect=dialect)).strip())
statements.append("""CREATE FUNCTION style_reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable_style_history'; END $$""")
immutable = (
    "audit_events",
    "style_card_versions",
    "style_card_sample_links",
    "style_template_slots",
    "product_assets",
    "product_asset_permission_events",
    "product_asset_links",
    "product_subject_seals",
    "image_candidates",
    "authenticity_checks",
    "authenticity_completions",
    "image_review_rounds",
    "image_review_session_jobs",
    "image_reviews",
    "approved_image_associations",
    "style_suite_completions",
    "style_suite_candidate_fidelities",
    "style_warehouse_v3_receipts",
    "mall_style_source_asset_bindings",
    "mall_style_subject_confirmations",
)
for name in immutable:
    if name not in Base.metadata.tables:
        raise RuntimeError(f"missing_frozen_table:{name}")
    statements.append(
        f"CREATE TRIGGER guard_{name[:40]} BEFORE UPDATE OR DELETE ON {name} "
        "FOR EACH ROW EXECUTE FUNCTION style_reject_history_mutation()"
    )
# Exact column whitelist; to_jsonb records preserve SQL NULL versus a value.
for table, mutable in {
    "image_output_jobs": [
        "status",
        "failure_reason",
        "identity_lock_phase",
        "generation_claim_token",
        "generation_lease_expires_at",
        "updated_at",
    ],
    "mall_style_handoff_receipts": ["status", "subject_seal_id", "updated_at"],
    "mall_style_generation_queue_items": [
        "status",
        "generation_job_id",
        "failure_reason",
        "updated_at",
    ],
    "real_image_pilot_authorizations": [
        "dispatch_state",
        "outbound_attempts",
        "updated_at",
    ],
}.items():
    permitted = ",".join(f"'{name}'" for name in mutable)
    body = f"""CREATE FUNCTION style_guard_{table}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
  IF (to_jsonb(OLD) - ARRAY[{permitted}]) IS DISTINCT FROM (to_jsonb(NEW) - ARRAY[{permitted}])
  THEN RAISE EXCEPTION 'immutable_style_lineage'; END IF;
"""
    if table == "mall_style_generation_queue_items":
        body += "  IF OLD.generation_job_id IS NOT NULL AND NEW.generation_job_id IS DISTINCT FROM OLD.generation_job_id THEN RAISE EXCEPTION 'immutable_generation_job'; END IF;\n"
    body += "  RETURN NEW;\nEND $$"
    statements.extend(
        [
            body,
            f"CREATE TRIGGER guard_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION style_guard_{table}()",
        ]
    )
statements.append("""CREATE FUNCTION style_guard_scene_origin() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN IF NEW.style_origin IS DISTINCT FROM OLD.style_origin THEN RAISE EXCEPTION 'scene_style_origin_immutable'; END IF; RETURN NEW; END $$""")
statements.append(
    "CREATE TRIGGER guard_scene_style_origin BEFORE UPDATE ON scene_styles FOR EACH ROW EXECUTE FUNCTION style_guard_scene_origin()"
)


statements.append("""CREATE FUNCTION style_guard_execution_fence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable_dispatch_fence'; END IF;
  IF NEW.queue_id IS DISTINCT FROM OLD.queue_id OR NEW.planned_job_id IS DISTINCT FROM OLD.planned_job_id OR NEW.id IS DISTINCT FROM OLD.id
    OR NEW.outbound_attempts < OLD.outbound_attempts OR (OLD.outbound_attempts = 1 AND (NEW.state = 'leased' OR NEW.claim_token IS DISTINCT FROM OLD.claim_token))
  THEN RAISE EXCEPTION 'immutable_dispatch_fence'; END IF;
  RETURN NEW;
END $$""")
statements.append(
    "CREATE TRIGGER guard_mall_style_execution BEFORE UPDATE OR DELETE ON mall_style_executions FOR EACH ROW EXECUTE FUNCTION style_guard_execution_fence()"
)
statements.append("""CREATE FUNCTION style_guard_sample_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'style_sample_content_immutable'; END IF;
  IF (to_jsonb(OLD) - 'withdrawn_at') IS DISTINCT FROM (to_jsonb(NEW) - 'withdrawn_at')
  THEN RAISE EXCEPTION 'style_sample_content_immutable'; END IF;
  RETURN NEW;
END $$""")
statements.append(
    "CREATE TRIGGER guard_style_sample_content BEFORE UPDATE OR DELETE ON style_samples FOR EACH ROW EXECUTE FUNCTION style_guard_sample_content()"
)
(root / "alembic_postgresql/versions/pg0001_baseline.sql").write_text(
    "\n-- statement --\n".join(statements) + "\n"
)
