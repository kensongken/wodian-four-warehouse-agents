"""persist controlled upload candidate authority snapshots

Revision ID: 0016_upload_binding_authority
Revises: 0015_upload_bindings
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_upload_binding_authority"
down_revision = "0015_upload_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "controlled_upload_bindings",
        sa.Column(
            "candidate_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_check_constraint(
        op.f("ck_controlled_upload_bindings_candidate_payload_object"),
        "controlled_upload_bindings",
        "candidate_payload IS NULL OR jsonb_typeof(candidate_payload) = 'object'",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE controlled_upload_bindings IN ACCESS EXCLUSIVE MODE")
    )
    has_authority = connection.execute(
        sa.text(
            "SELECT 1 FROM controlled_upload_bindings "
            "WHERE candidate_payload IS NOT NULL LIMIT 1"
        )
    ).scalar()
    if has_authority is not None:
        raise RuntimeError(
            "cannot downgrade 0016_upload_binding_authority: controlled upload "
            "candidate authority exists; export or explicitly delete it first"
        )
    op.drop_constraint(
        op.f("ck_controlled_upload_bindings_candidate_payload_object"),
        "controlled_upload_bindings",
        type_="check",
    )
    op.drop_column("controlled_upload_bindings", "candidate_payload")
