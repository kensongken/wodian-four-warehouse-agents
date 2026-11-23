"""add OPC workspaces and memberships

Revision ID: 0011_opc_memberships
Revises: 0010_classification_reviews
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_opc_memberships"
down_revision = "0010_classification_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opc_workspaces",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(id) BETWEEN 3 AND 128 "
            "AND id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name=op.f("ck_opc_workspaces_id_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(display_name)) BETWEEN 1 AND 128",
            name=op.f("ck_opc_workspaces_display_name_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opc_workspaces")),
    )
    op.create_table(
        "opc_memberships",
        sa.Column("opc_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'reviewer')",
            name=op.f("ck_opc_memberships_role_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ("opc_id",),
            ("opc_workspaces.id",),
            name=op.f("fk_opc_memberships_opc_id_opc_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("user_id",),
            ("users.id",),
            name=op.f("fk_opc_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opc_memberships")),
        sa.UniqueConstraint(
            "opc_id",
            "user_id",
            name="opc_memberships_opc_user_key",
        ),
    )
    op.create_index(
        "ix_opc_memberships_user_id",
        "opc_memberships",
        ("user_id",),
        unique=False,
    )


def downgrade() -> None:
    has_authority_data = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM opc_memberships "
                "UNION ALL SELECT 1 FROM opc_workspaces LIMIT 1"
            )
        )
        .scalar()
    )
    if has_authority_data is not None:
        raise RuntimeError(
            "cannot downgrade 0011_opc_memberships: OPC authority data exists; "
            "export or explicitly delete it first"
        )
    op.drop_index("ix_opc_memberships_user_id", table_name="opc_memberships")
    op.drop_table("opc_memberships")
    op.drop_table("opc_workspaces")
