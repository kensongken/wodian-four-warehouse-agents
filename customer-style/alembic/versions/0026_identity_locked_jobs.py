"""Attach immutable subject seals and phases to output jobs."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_identity_locked_jobs"
down_revision: str | None = "0025_product_subject_seals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PHASE_SQL = (
    "identity_lock_phase IN ('legacy', 'subject_sealed', 'backgrounds_generated', "
    "'backgrounds_safe', 'candidates_attested', 'generation_failed', "
    "'generation_uncertain')"
)


def upgrade() -> None:
    connection = op.get_bind()
    existing_foreign_key_violations = _foreign_key_violations()
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            triggers = _suspend_image_output_job_triggers()
            with op.batch_alter_table("image_output_jobs") as batch:
                batch.add_column(
                    sa.Column("subject_seal_id", sa.String(36), nullable=True)
                )
                batch.add_column(
                    sa.Column(
                        "identity_lock_phase",
                        sa.String(32),
                        nullable=False,
                        server_default="legacy",
                    )
                )
                batch.create_foreign_key(
                    "fk_image_output_jobs_customer_subject_seal",
                    "product_subject_seals",
                    ["customer_id", "subject_seal_id"],
                    ["customer_id", "id"],
                )
                batch.create_check_constraint(
                    "ck_image_output_jobs_identity_lock_phase", PHASE_SQL
                )
                batch.create_index(
                    "ix_image_output_jobs_subject_seal_id", ["subject_seal_id"]
                )
            _restore_image_output_job_triggers(triggers, allow_uncertain=True)
            _create_identity_lock_triggers()
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if _foreign_key_violations() - existing_foreign_key_violations:
            raise RuntimeError("identity lock migration foreign key check failed")


def downgrade() -> None:
    connection = op.get_bind()
    existing_foreign_key_violations = _foreign_key_violations()
    count = connection.execute(
        sa.text(
            "SELECT count(*) FROM image_output_jobs WHERE subject_seal_id IS NOT NULL"
        )
    ).scalar_one()
    if count:
        raise RuntimeError("cannot discard identity-locked job evidence")
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            op.execute(
                "DROP TRIGGER IF EXISTS trg_image_output_jobs_identity_lock_lineage"
            )
            op.execute("DROP TRIGGER IF EXISTS trg_image_output_jobs_identity_lock_phase")
            triggers = _suspend_image_output_job_triggers()
            with op.batch_alter_table("image_output_jobs") as batch:
                batch.drop_index("ix_image_output_jobs_subject_seal_id")
                batch.drop_constraint(
                    "ck_image_output_jobs_identity_lock_phase", type_="check"
                )
                batch.drop_constraint(
                    "fk_image_output_jobs_customer_subject_seal", type_="foreignkey"
                )
                batch.drop_column("identity_lock_phase")
                batch.drop_column("subject_seal_id")
            _restore_image_output_job_triggers(triggers, allow_uncertain=False)
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if _foreign_key_violations() - existing_foreign_key_violations:
            raise RuntimeError("identity lock downgrade foreign key check failed")


def _foreign_key_violations() -> set[tuple[object, ...]]:
    """Return the exact SQLite violations so this migration rejects only new damage."""
    return {tuple(row) for row in op.get_bind().exec_driver_sql("PRAGMA foreign_key_check")}


def _suspend_image_output_job_triggers() -> list[tuple[str, str]]:
    """SQLite table-copy migrations require temporarily removing dependent triggers."""
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND sql LIKE '%image_output_jobs%' AND sql IS NOT NULL"
        )
    ).all()
    result = [(str(name), str(sql)) for name, sql in rows]
    for name, _ in result:
        op.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    return result


def _restore_image_output_job_triggers(
    triggers: list[tuple[str, str]], *, allow_uncertain: bool
) -> None:
    for name, sql in triggers:
        if name in {
            "trg_image_output_jobs_identity_lock_lineage",
            "trg_image_output_jobs_identity_lock_phase",
        }:
            continue
        if name == "trg_image_output_jobs_status_transition":
            if allow_uncertain:
                sql = sql.replace(
                    "'machine_check', 'generation_failed', 'cleanup_pending'",
                    "'machine_check', 'generation_failed', 'generation_uncertain', "
                    "'cleanup_pending'",
                )
            else:
                sql = sql.replace("'generation_uncertain', ", "")
        op.execute(sa.text(sql))


def _create_identity_lock_triggers() -> None:
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_identity_lock_lineage "
        "BEFORE UPDATE ON image_output_jobs "
        "WHEN OLD.subject_seal_id IS NOT NULL AND ("
        "NEW.subject_seal_id IS NOT OLD.subject_seal_id "
        "OR NEW.customer_id IS NOT OLD.customer_id "
        "OR NEW.style_card_version_id IS NOT OLD.style_card_version_id "
        "OR NEW.scene_style_id IS NOT OLD.scene_style_id "
        "OR NEW.output_ratio IS NOT OLD.output_ratio "
        "OR NEW.idempotency_key IS NOT OLD.idempotency_key) "
        "BEGIN SELECT RAISE(ABORT, 'identity_locked_job_lineage_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_identity_lock_phase "
        "BEFORE UPDATE OF identity_lock_phase ON image_output_jobs "
        "WHEN NEW.identity_lock_phase NOT IN "
        "('legacy', 'subject_sealed', 'backgrounds_generated', 'backgrounds_safe', "
        "'candidates_attested', 'generation_failed', 'generation_uncertain') "
        "BEGIN SELECT RAISE(ABORT, 'identity_lock_phase_invalid'); END"
    )
