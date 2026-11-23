"""persist authenticated upload candidate bindings

Revision ID: 0015_upload_bindings
Revises: 0014_snapshot_evidence_mode
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_upload_bindings"
down_revision = "0014_snapshot_evidence_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "import_tasks",
        sa.Column("opc_id", sa.String(length=128), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_import_tasks_opc_id_opc_workspaces"),
        "import_tasks",
        "opc_workspaces",
        ("opc_id",),
        ("id",),
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "import_tasks_id_opc_id_key",
        "import_tasks",
        ("id", "opc_id"),
    )
    op.create_index(
        op.f("ix_import_tasks_opc_id"),
        "import_tasks",
        ("opc_id",),
        unique=False,
    )
    op.create_table(
        "controlled_upload_bindings",
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("source_readiness_id", sa.String(length=128), nullable=False),
        sa.Column("opc_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("album_product_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
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
            "candidate_id ~ '^opc-controlled-upload-candidate:[0-9a-f]{64}$'",
            name=op.f("ck_controlled_upload_bindings_candidate_id_allowed"),
        ),
        sa.CheckConstraint(
            "source_readiness_id ~ '^opc-real-sku-pilot-readiness:[0-9a-f]{64}$'",
            name=op.f("ck_controlled_upload_bindings_source_readiness_id_allowed"),
        ),
        sa.CheckConstraint(
            "source_type = 'first_sale_bead'",
            name=op.f("ck_controlled_upload_bindings_source_type_allowed"),
        ),
        sa.CheckConstraint(
            "item_count BETWEEN 2 AND 4",
            name=op.f("ck_controlled_upload_bindings_item_count_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ("task_id", "opc_id"),
            ("import_tasks.id", "import_tasks.opc_id"),
            name="fk_controlled_upload_bindings_task_id_opc_id_import_tasks",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("user_id", "task_id"),
            ("import_tasks.user_id", "import_tasks.id"),
            name="fk_controlled_upload_bindings_user_id_task_id_import_tasks",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_controlled_upload_bindings")),
        sa.UniqueConstraint(
            "candidate_id",
            name="controlled_upload_bindings_candidate_id_key",
        ),
    )
    op.create_index(
        "ix_controlled_upload_bindings_opc_id_task_id",
        "controlled_upload_bindings",
        ("opc_id", "task_id"),
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "LOCK TABLE controlled_upload_bindings, import_tasks "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    has_bindings = connection.execute(
        sa.text("SELECT 1 FROM controlled_upload_bindings LIMIT 1")
    ).scalar()
    has_task_authority = connection.execute(
        sa.text("SELECT 1 FROM import_tasks WHERE opc_id IS NOT NULL LIMIT 1")
    ).scalar()
    if has_bindings is not None or has_task_authority is not None:
        raise RuntimeError(
            "cannot downgrade 0015_upload_bindings: "
            "authenticated upload binding authority exists; export or explicitly "
            "delete bindings and clear task OPC ownership first"
        )
    op.drop_index(
        "ix_controlled_upload_bindings_opc_id_task_id",
        table_name="controlled_upload_bindings",
    )
    op.drop_table("controlled_upload_bindings")
    op.drop_index(op.f("ix_import_tasks_opc_id"), table_name="import_tasks")
    op.drop_constraint(
        "import_tasks_id_opc_id_key",
        "import_tasks",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_import_tasks_opc_id_opc_workspaces"),
        "import_tasks",
        type_="foreignkey",
    )
    op.drop_column("import_tasks", "opc_id")
