"""Permanently bind customer output jobs to a single review session."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "0014_review_session_jobs"
down_revision: str | None = "0013_review_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Inspect before DDL: ambiguous historical ownership needs reconciliation,
    # never an arbitrary reassignment or deletion of an immutable decision.
    ownership = list(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT customer_id, current_job_id AS job_id, id AS session_id "
                "FROM image_review_sessions UNION "
                "SELECT customer_id, output_job_id AS job_id, session_id FROM image_review_rounds"
            )
        )
        .mappings()
    )
    owners: dict[tuple[str, str], str] = {}
    for row in ownership:
        key = (row["customer_id"], row["job_id"])
        previous = owners.setdefault(key, row["session_id"])
        if previous != row["session_id"]:
            raise RuntimeError(
                "ambiguous review session job ownership; reconcile before upgrade"
            )
    table = op.create_table(
        "image_review_session_jobs",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id", "session_id"],
            ["image_review_sessions.customer_id", "image_review_sessions.id"],
            name="fk_image_review_session_jobs_customer_session",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_review_session_jobs_customer_job",
        ),
        sa.UniqueConstraint(
            "customer_id", "job_id", name="uq_review_session_jobs_customer_job"
        ),
    )
    op.create_index(
        "ix_review_session_jobs_customer_session",
        "image_review_session_jobs",
        ["customer_id", "session_id"],
    )
    if owners:
        op.bulk_insert(
            table,
            [
                {
                    "id": str(uuid4()),
                    "customer_id": customer_id,
                    "job_id": job_id,
                    "session_id": session_id,
                    "created_at": datetime.now(UTC),
                }
                for (customer_id, job_id), session_id in owners.items()
            ],
        )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_image_review_session_jobs_no_{action.lower()} "
            f"BEFORE {action} ON image_review_session_jobs "
            "BEGIN SELECT RAISE(ABORT, 'image_review_session_job_immutable'); END"
        )


def downgrade() -> None:
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM image_review_session_jobs"))
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "cannot discard customer image review job bindings while rows exist"
        )
    for action in ("update", "delete"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_image_review_session_jobs_no_{action}")
    op.drop_table("image_review_session_jobs")
