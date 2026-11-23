"""Persist customer candidate-image review sessions and immutable rounds."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision: str = "0013_review_sessions"
down_revision: str | None = "0012_classification_bridge_claim_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _create_round_immutability_triggers() -> None:
    for trigger in (
        "trg_image_review_rounds_no_update",
        "trg_image_review_rounds_no_delete",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(
        "CREATE TRIGGER trg_image_review_rounds_no_update "
        "BEFORE UPDATE ON image_review_rounds "
        "BEGIN SELECT RAISE(ABORT, 'image_review_round_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_image_review_rounds_no_delete "
        "BEFORE DELETE ON image_review_rounds "
        "BEGIN SELECT RAISE(ABORT, 'image_review_round_immutable'); END"
    )


def upgrade() -> None:
    """Create the mutable workflow cursor and append-only round history."""
    op.create_table(
        "image_review_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=128), nullable=False),
        sa.Column("initial_handoff_id", sa.String(length=36), nullable=False),
        sa.Column("current_job_id", sa.String(length=36), nullable=False),
        sa.Column(
            "regeneration_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "status",
            sa.String(length=40),
            nullable=False,
            server_default="awaiting_review",
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_owner_token", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "regeneration_count BETWEEN 0 AND 3",
            name="ck_image_review_sessions_regeneration_count",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_image_review_sessions_revision"),
        sa.CheckConstraint(
            "(status = 'regenerating' AND claim_owner_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL) "
            "OR (status != 'regenerating' AND claim_owner_token IS NULL "
            "AND claimed_at IS NULL AND claim_expires_at IS NULL)",
            name="ck_image_review_sessions_claim_state",
        ),
        sa.CheckConstraint(
            "claim_expires_at IS NULL OR claim_expires_at >= claimed_at",
            name="ck_image_review_sessions_claim_expiry",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "current_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_review_sessions_customer_job",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("initial_handoff_id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_image_review_sessions_customer_id_id"
        ),
    )
    op.create_index(
        "ix_image_review_sessions_customer_id",
        "image_review_sessions",
        ["customer_id"],
    )
    op.create_index(
        "ix_image_review_sessions_customer_product",
        "image_review_sessions",
        ["customer_id", "product_id"],
    )
    op.create_table(
        "image_review_rounds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("output_job_id", sa.String(length=36), nullable=False),
        sa.Column("image_review_id", sa.String(length=36), nullable=False),
        sa.Column("image_candidate_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "round_number BETWEEN 1 AND 4",
            name="ck_image_review_rounds_number",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "session_id"],
            ["image_review_sessions.customer_id", "image_review_sessions.id"],
            name="fk_image_review_rounds_customer_session",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_review_rounds_customer_job",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "image_review_id", "image_candidate_id"],
            [
                "image_reviews.customer_id",
                "image_reviews.id",
                "image_reviews.image_candidate_id",
            ],
            name="fk_image_review_rounds_customer_review_candidate",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "round_number", name="uq_image_review_rounds_session_number"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_image_review_rounds_customer_idempotency",
        ),
    )
    op.create_index(
        "ix_image_review_rounds_customer_id",
        "image_review_rounds",
        ["customer_id"],
    )
    op.create_index(
        "ix_image_review_rounds_customer_session",
        "image_review_rounds",
        ["customer_id", "session_id"],
    )
    _create_round_immutability_triggers()


def downgrade() -> None:
    """Refuse to erase any customer review workflow or audit-round history."""
    table_names = _table_names()
    for table_name in ("image_review_rounds", "image_review_sessions"):
        if table_name in table_names:
            count = op.get_bind().execute(
                text(f"SELECT count(*) FROM {table_name}")
            ).scalar_one()
            if count:
                raise RuntimeError(
                    "cannot discard customer image review sessions or round history "
                    "while rows exist"
                )
    for trigger in (
        "trg_image_review_rounds_no_delete",
        "trg_image_review_rounds_no_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    if "image_review_rounds" in table_names:
        op.drop_table("image_review_rounds")
    if "image_review_sessions" in table_names:
        op.drop_table("image_review_sessions")
