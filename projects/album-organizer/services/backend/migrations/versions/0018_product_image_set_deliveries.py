"""add ProductImageSetReady authority and replay ledger

Revision ID: 0018_image_set_delivery
Revises: 0017_task_duplicate_receipts
Create Date: 2026-08-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_image_set_delivery"
down_revision = "0017_task_duplicate_receipts"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column[object], sa.Column[object], sa.Column[object]]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "opc_external_tenant_bindings",
        sa.Column("opc_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("binding_source", sa.String(length=32), nullable=False),
        sa.Column("provisioned_by", sa.String(length=128), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "binding_source = 'operator_provisioned'",
            name=op.f("ck_opc_external_tenant_bindings_binding_source_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(provisioned_by)) BETWEEN 3 AND 128",
            name=op.f("ck_opc_external_tenant_bindings_provisioned_by_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["opc_id"],
            ["opc_workspaces.id"],
            name=op.f("fk_opc_external_tenant_bindings_opc_id_opc_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opc_external_tenant_bindings")),
        sa.UniqueConstraint("opc_id", name="opc_external_tenant_bindings_opc_key"),
        sa.UniqueConstraint(
            "tenant_id", name="opc_external_tenant_bindings_tenant_key"
        ),
    )
    op.create_table(
        "product_code_bindings",
        sa.Column("opc_id", sa.String(length=128), nullable=False),
        sa.Column("album_product_id", sa.String(length=128), nullable=False),
        sa.Column("product_code", sa.String(length=64), nullable=False),
        sa.Column("binding_source", sa.String(length=32), nullable=False),
        sa.Column("provisioned_by", sa.String(length=128), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "product_code ~ '^[A-Za-z0-9_-]{1,64}$'",
            name=op.f("ck_product_code_bindings_product_code_allowed"),
        ),
        sa.CheckConstraint(
            "binding_source = 'operator_provisioned'",
            name=op.f("ck_product_code_bindings_binding_source_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(provisioned_by)) BETWEEN 3 AND 128",
            name=op.f("ck_product_code_bindings_provisioned_by_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["opc_id"],
            ["opc_workspaces.id"],
            name=op.f("fk_product_code_bindings_opc_id_opc_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_code_bindings")),
        sa.UniqueConstraint(
            "opc_id",
            "album_product_id",
            name="product_code_bindings_product_key",
        ),
        sa.UniqueConstraint(
            "opc_id", "product_code", name="product_code_bindings_code_key"
        ),
    )
    op.create_table(
        "prepared_image_handoffs",
        sa.Column("product_binding_id", sa.Uuid(), nullable=False),
        sa.Column("source_classification_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("quality_confidence", sa.Float(), nullable=False),
        sa.Column("storage_status", sa.String(length=32), nullable=False),
        sa.Column("prepared_by", sa.String(length=128), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "role IN ('main_candidate', 'gallery', 'source_original')",
            name=op.f("ck_prepared_image_handoffs_role_allowed"),
        ),
        sa.CheckConstraint(
            "quality_confidence BETWEEN 0 AND 1",
            name=op.f("ck_prepared_image_handoffs_quality_confidence_allowed"),
        ),
        sa.CheckConstraint(
            "storage_status = 'ready'",
            name=op.f("ck_prepared_image_handoffs_storage_status_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(prepared_by)) BETWEEN 3 AND 128",
            name=op.f("ck_prepared_image_handoffs_prepared_by_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["product_binding_id"],
            ["product_code_bindings.id"],
            name=op.f(
                "fk_prepared_image_handoffs_product_binding_id_product_code_bindings"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_classification_id"],
            ["product_classifications.id"],
            name=op.f(
                "fk_prepared_image_handoffs_source_classification_id_product_classifications"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prepared_image_handoffs")),
        sa.UniqueConstraint(
            "source_classification_id",
            "asset_id",
            name="prepared_image_handoffs_classification_asset_key",
        ),
        sa.UniqueConstraint(
            "object_key", name="prepared_image_handoffs_object_key_key"
        ),
    )
    op.create_table(
        "product_image_set_deliveries",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("source_classification_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_binding_id", sa.Uuid(), nullable=False),
        sa.Column("product_binding_id", sa.Uuid(), nullable=False),
        sa.Column("prepared_image_binding_ids", sa.JSON(), nullable=False),
        sa.Column("canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_product_image_set_deliveries_canonical_sha256_lowercase_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["source_classification_id"],
            ["product_classifications.id"],
            name=op.f(
                "fk_product_image_set_deliveries_source_classification_id_product_classifications"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"],
            ["album_product_snapshots.id"],
            name=op.f(
                "fk_product_image_set_deliveries_source_snapshot_id_album_product_snapshots"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_binding_id"],
            ["opc_external_tenant_bindings.id"],
            name=op.f(
                "fk_product_image_set_deliveries_tenant_binding_id_opc_external_tenant_bindings"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_binding_id"],
            ["product_code_bindings.id"],
            name=op.f(
                "fk_product_image_set_deliveries_product_binding_id_product_code_bindings"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_image_set_deliveries")),
        sa.UniqueConstraint("event_id", name="product_image_set_deliveries_event_key"),
        sa.UniqueConstraint(
            "source_classification_id",
            name="product_image_set_deliveries_classification_key",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION reject_handoff_authority_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'handoff authority records are immutable';
        END;
        $$
        """
    )
    for table in (
        "opc_external_tenant_bindings",
        "product_code_bindings",
        "prepared_image_handoffs",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable_update
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_handoff_authority_update()
            """
        )
    op.create_table(
        "product_image_set_delivery_images",
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("prepared_image_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["product_image_set_deliveries.id"],
            name=op.f(
                "fk_product_image_set_delivery_images_delivery_id_product_image_set_deliveries"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prepared_image_id"],
            ["prepared_image_handoffs.id"],
            name=op.f(
                "fk_product_image_set_delivery_images_prepared_image_id_prepared_image_handoffs"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_product_image_set_delivery_images")
        ),
        sa.UniqueConstraint(
            "delivery_id",
            "prepared_image_id",
            name="product_image_set_delivery_images_pair_key",
        ),
        sa.UniqueConstraint(
            "prepared_image_id",
            name="product_image_set_delivery_images_prepared_key",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "LOCK TABLE product_image_set_delivery_images, "
            "product_image_set_deliveries, prepared_image_handoffs, "
            "product_code_bindings, opc_external_tenant_bindings "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    for table in (
        "product_image_set_delivery_images",
        "product_image_set_deliveries",
        "prepared_image_handoffs",
        "product_code_bindings",
        "opc_external_tenant_bindings",
    ):
        has_evidence = connection.execute(
            sa.text(f"SELECT 1 FROM {table} LIMIT 1")
        ).scalar()
        if has_evidence is not None:
            raise RuntimeError(
                "cannot downgrade 0018_image_set_delivery: handoff authority or "
                "delivery evidence exists; export or explicitly delete it first"
            )
    op.drop_table("product_image_set_delivery_images")
    op.drop_table("product_image_set_deliveries")
    op.drop_table("prepared_image_handoffs")
    op.drop_table("product_code_bindings")
    op.drop_table("opc_external_tenant_bindings")
    op.execute("DROP FUNCTION reject_handoff_authority_update()")
