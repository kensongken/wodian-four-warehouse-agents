"""Seal authorized historical style samples to immutable media facts."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_style_sample_content"
down_revision: str | None = "0015_review_session_initial_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add optional seals without invalidating legacy consent-only records."""
    connection = op.get_bind()
    # SQLite only supports adding these safeguards by copying the one bounded
    # table. Disable FK enforcement just for the rebuild, as in migration 0015.
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("style_samples", recreate="always") as batch:
                batch.add_column(sa.Column("content_sha256", sa.String(64)))
                batch.add_column(sa.Column("mime_type", sa.String(100)))
                batch.add_column(sa.Column("width", sa.Integer()))
                batch.add_column(sa.Column("height", sa.Integer()))
                batch.add_column(
                    sa.Column("asset_store_namespace", sa.String(255))
                )
                batch.create_check_constraint(
                    "ck_style_samples_content_sha256_length",
                    "content_sha256 IS NULL OR length(content_sha256) = 64",
                )
                batch.create_check_constraint(
                    "ck_style_samples_content_dimensions_positive",
                    "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
                )
                batch.create_check_constraint(
                    "ck_style_samples_content_seal_complete",
                    "(content_sha256 IS NULL AND mime_type IS NULL AND width IS NULL "
                    "AND height IS NULL AND asset_store_namespace IS NULL) OR "
                    "(content_sha256 IS NOT NULL AND mime_type IS NOT NULL AND width "
                    "IS NOT NULL AND height IS NOT NULL AND asset_store_namespace "
                    "IS NOT NULL)",
                )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("style sample content migration foreign key check failed")
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


def downgrade() -> None:
    """Refuse to discard active historical media seals."""
    if op.get_bind().execute(
        sa.text("SELECT count(*) FROM style_samples WHERE content_sha256 IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("cannot discard sealed style sample content while rows exist")
    op.execute("DROP TRIGGER trg_style_samples_content_immutable")
    with op.batch_alter_table("style_samples") as batch:
        batch.drop_constraint("ck_style_samples_content_seal_complete", type_="check")
        batch.drop_constraint(
            "ck_style_samples_content_dimensions_positive", type_="check"
        )
        batch.drop_constraint("ck_style_samples_content_sha256_length", type_="check")
        batch.drop_column("asset_store_namespace")
        batch.drop_column("height")
        batch.drop_column("width")
        batch.drop_column("mime_type")
        batch.drop_column("content_sha256")
