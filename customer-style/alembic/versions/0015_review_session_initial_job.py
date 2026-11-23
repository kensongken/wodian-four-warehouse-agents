"""Give every initial reviewable output its own immutable session identity."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_review_session_initial_job"
down_revision: str | None = "0014_review_session_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    # Determine every original job from immutable round one, or the unchanged
    # cursor of an unreviewed session. Validate before changing the schema.
    roots = list(
        connection.execute(
            sa.text(
                "SELECT s.id, s.customer_id, COALESCE(r.output_job_id, s.current_job_id) AS job_id "
                "FROM image_review_sessions s LEFT JOIN image_review_rounds r "
                "ON r.session_id=s.id AND r.customer_id=s.customer_id AND r.round_number=1"
            )
        ).mappings()
    )
    if len({row["job_id"] for row in roots}) != len(roots):
        raise RuntimeError(
            "ambiguous initial review job ownership; reconcile before upgrade"
        )
    for row in roots:
        owner = connection.execute(
            sa.text(
                "SELECT session_id FROM image_review_session_jobs "
                "WHERE customer_id=:customer_id AND job_id=:job_id"
            ),
            row,
        ).scalar_one_or_none()
        if owner != row["id"]:
            raise RuntimeError(
                "missing initial review job binding; reconcile before upgrade"
            )

    # SQLite cannot drop its unnamed handoff UNIQUE constraint in place. Alembic
    # rebuilds only this cursor table; FK checks are disabled for that bounded
    # rebuild and restored before returning. Child history is never rewritten.
    with op.get_context().autocommit_block():
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            op.add_column(
                "image_review_sessions", sa.Column("initial_job_id", sa.String(36))
            )
            for row in roots:
                connection.execute(
                    sa.text(
                        "UPDATE image_review_sessions SET initial_job_id=:job_id WHERE id=:id"
                    ),
                    row,
                )
            with op.batch_alter_table(
                "image_review_sessions",
                recreate="always",
                naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
            ) as batch:
                batch.drop_constraint(
                    "uq_image_review_sessions_initial_handoff_id", type_="unique"
                )
                batch.alter_column(
                    "initial_job_id", existing_type=sa.String(36), nullable=False
                )
                batch.create_unique_constraint(
                    "uq_image_review_sessions_initial_job", ["initial_job_id"]
                )
                batch.create_foreign_key(
                    "fk_image_review_sessions_customer_initial_job",
                    "image_output_jobs",
                    ["customer_id", "initial_job_id"],
                    ["customer_id", "id"],
                )
            op.execute(
                "CREATE TRIGGER trg_image_review_sessions_identity_immutable "
                "BEFORE UPDATE OF customer_id, initial_job_id, initial_handoff_id, product_id "
                "ON image_review_sessions WHEN NEW.customer_id != OLD.customer_id "
                "OR NEW.initial_job_id != OLD.initial_job_id "
                "OR NEW.initial_handoff_id != OLD.initial_handoff_id "
                "OR NEW.product_id != OLD.product_id "
                "BEGIN SELECT RAISE(ABORT, 'image_review_session_identity_immutable'); END"
            )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("review session migration foreign key check failed")


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM image_review_sessions"))
        .scalar_one()
    ):
        raise RuntimeError(
            "cannot discard customer image review initial job identities while rows exist"
        )
    op.execute("DROP TRIGGER IF EXISTS trg_image_review_sessions_identity_immutable")
    with op.batch_alter_table("image_review_sessions", recreate="always") as batch:
        batch.drop_constraint(
            "fk_image_review_sessions_customer_initial_job", type_="foreignkey"
        )
        batch.drop_constraint("uq_image_review_sessions_initial_job", type_="unique")
        batch.drop_column("initial_job_id")
        batch.create_unique_constraint(
            "uq_image_review_sessions_initial_handoff_id", ["initial_handoff_id"]
        )
