"""Bind each sealed style sample to the legal text version it authorized."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_style_sample_consent_version"
down_revision: str | None = "0016_style_sample_content"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill durable audit evidence, then seal consent facts at the database layer."""
    connection = op.get_bind()
    unresolved = connection.execute(
        sa.text(
            "SELECT count(*) FROM style_samples WHERE content_sha256 IS NOT NULL "
            "AND trim(COALESCE((SELECT json_extract(audit_events.payload, "
            "'$.consent_text_version') FROM audit_events "
            "WHERE audit_events.customer_id = style_samples.customer_id "
            "AND audit_events.event_type = 'style_sample.content_authorized' "
            "AND json_extract(audit_events.payload, '$.sample_id') = style_samples.id "
            "ORDER BY audit_events.occurred_at DESC, audit_events.id DESC LIMIT 1), "
            "'')) = ''"
        )
    ).scalar_one()
    if unresolved:
        raise RuntimeError("sealed style sample consent text version cannot be recovered")
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("style_samples", recreate="always") as batch:
                batch.add_column(sa.Column("consent_text_version", sa.String(100)))
            connection.execute(
                sa.text(
                    "UPDATE style_samples SET consent_text_version = ("
                    "SELECT json_extract(audit_events.payload, '$.consent_text_version') "
                    "FROM audit_events WHERE audit_events.customer_id = style_samples.customer_id "
                    "AND audit_events.event_type = 'style_sample.content_authorized' "
                    "AND json_extract(audit_events.payload, '$.sample_id') = style_samples.id "
                    "ORDER BY audit_events.occurred_at DESC, audit_events.id DESC LIMIT 1"
                    ") WHERE content_sha256 IS NOT NULL"
                )
            )
            with op.batch_alter_table("style_samples", recreate="always") as batch:
                batch.create_check_constraint(
                    "ck_style_samples_content_consent_complete",
                    "(content_sha256 IS NULL AND consent_text_version IS NULL) OR "
                    "(content_sha256 IS NOT NULL AND consent_text_version IS NOT NULL "
                    "AND length(trim(consent_text_version)) > 0)",
                )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("style sample consent version migration foreign key check failed")
    # SQLite batch rebuilds discard table triggers, so tolerate either state.
    op.execute("DROP TRIGGER IF EXISTS trg_style_samples_content_immutable")
    op.execute(
        "CREATE TRIGGER trg_style_samples_content_immutable "
        "BEFORE UPDATE OF customer_id, photo_id, consent_provenance, "
        "consent_text_version, content_sha256, mime_type, width, height, "
        "asset_store_namespace ON style_samples "
        "WHEN NEW.customer_id IS NOT OLD.customer_id "
        "OR NEW.photo_id IS NOT OLD.photo_id "
        "OR NEW.consent_provenance IS NOT OLD.consent_provenance "
        "OR NEW.consent_text_version IS NOT OLD.consent_text_version "
        "OR NEW.content_sha256 IS NOT OLD.content_sha256 "
        "OR NEW.mime_type IS NOT OLD.mime_type "
        "OR NEW.width IS NOT OLD.width "
        "OR NEW.height IS NOT OLD.height "
        "OR NEW.asset_store_namespace IS NOT OLD.asset_store_namespace "
        "BEGIN SELECT RAISE(ABORT, 'style_sample_content_immutable'); END"
    )


def downgrade() -> None:
    """Do not discard legal evidence from sealed rows."""
    if op.get_bind().execute(
        sa.text("SELECT count(*) FROM style_samples WHERE content_sha256 IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("cannot discard sealed style sample consent versions")
    op.execute("DROP TRIGGER trg_style_samples_content_immutable")
    with op.batch_alter_table("style_samples", recreate="always") as batch:
        batch.drop_constraint(
            "ck_style_samples_content_consent_complete", type_="check"
        )
        batch.drop_column("consent_text_version")
    op.execute(
        "CREATE TRIGGER trg_style_samples_content_immutable "
        "BEFORE UPDATE OF customer_id, photo_id, content_sha256, mime_type, width, "
        "height, asset_store_namespace ON style_samples "
        "WHEN NEW.customer_id IS NOT OLD.customer_id "
        "OR NEW.photo_id IS NOT OLD.photo_id "
        "OR NEW.content_sha256 IS NOT OLD.content_sha256 "
        "OR NEW.mime_type IS NOT OLD.mime_type "
        "OR NEW.width IS NOT OLD.width "
        "OR NEW.height IS NOT OLD.height "
        "OR NEW.asset_store_namespace IS NOT OLD.asset_store_namespace "
        "BEGIN SELECT RAISE(ABORT, 'style_sample_content_immutable'); END"
    )
