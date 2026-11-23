"""Add immutable asset-recovery and style-field evidence ledgers."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_asset_store_recovery_and_style_evidence"
down_revision: str | None = "0024_scene_style_origin_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset_store_recovery_bindings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("source_namespace", sa.String(64), nullable=False),
        sa.Column("target_namespace", sa.String(64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("source_label", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.UniqueConstraint(
            "customer_id",
            "source_namespace",
            "target_namespace",
            "manifest_sha256",
            name="uq_asset_store_recovery_binding_manifest",
        ),
        sa.CheckConstraint(
            "length(source_namespace) = 64 AND source_namespace = lower(source_namespace)",
            name="ck_asset_store_recovery_source_namespace",
        ),
        sa.CheckConstraint(
            "length(target_namespace) = 64 AND target_namespace = lower(target_namespace)",
            name="ck_asset_store_recovery_target_namespace",
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64 AND manifest_sha256 = lower(manifest_sha256)",
            name="ck_asset_store_recovery_manifest_sha256",
        ),
        sa.CheckConstraint(
            "sample_count > 0", name="ck_asset_store_recovery_sample_count"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_asset_store_recovery_status",
        ),
    )
    op.create_index(
        "ix_asset_store_recovery_bindings_customer_id",
        "asset_store_recovery_bindings",
        ["customer_id"],
    )
    op.create_table(
        "style_field_evidence",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("style_card_version_id", sa.String(36), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("field_value", sa.String(200), nullable=False),
        sa.Column("style_sample_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_card_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_field_evidence_customer_version",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_sample_id"],
            ["style_samples.customer_id", "style_samples.id"],
            name="fk_style_field_evidence_customer_sample",
        ),
        sa.UniqueConstraint(
            "style_card_version_id",
            "field_name",
            "field_value",
            "style_sample_id",
            name="uq_style_field_evidence_fact_sample",
        ),
        sa.CheckConstraint(
            "field_name IN ('tone', 'palette', 'lighting', 'composition', "
            "'backgrounds', 'people_and_props', 'material_treatment', "
            "'typography', 'forbidden_elements')",
            name="ck_style_field_evidence_field_name",
        ),
        sa.CheckConstraint(
            "length(trim(field_value)) > 0",
            name="ck_style_field_evidence_value_nonempty",
        ),
    )
    op.create_index(
        "ix_style_field_evidence_customer_id",
        "style_field_evidence",
        ["customer_id"],
    )
    op.create_index(
        "ix_style_field_evidence_style_card_version_id",
        "style_field_evidence",
        ["style_card_version_id"],
    )
    op.create_index(
        "ix_style_field_evidence_style_sample_id",
        "style_field_evidence",
        ["style_sample_id"],
    )
    for table, code in (
        ("asset_store_recovery_bindings", "asset_store_recovery_binding_immutable"),
        ("style_field_evidence", "style_field_evidence_immutable"),
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table.removesuffix('s')}_immutable "
            f"BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{code}'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table.removesuffix('s')}_no_delete "
            f"BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{code}'); END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("asset_store_recovery_bindings", "style_field_evidence"):
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one():
            raise RuntimeError(f"cannot discard append-only evidence from {table}")
    for trigger in (
        "trg_style_field_evidence_no_delete",
        "trg_style_field_evidence_immutable",
        "trg_asset_store_recovery_binding_no_delete",
        "trg_asset_store_recovery_binding_immutable",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index(
        "ix_style_field_evidence_style_sample_id", table_name="style_field_evidence"
    )
    op.drop_index(
        "ix_style_field_evidence_style_card_version_id",
        table_name="style_field_evidence",
    )
    op.drop_index("ix_style_field_evidence_customer_id", table_name="style_field_evidence")
    op.drop_table("style_field_evidence")
    op.drop_index(
        "ix_asset_store_recovery_bindings_customer_id",
        table_name="asset_store_recovery_bindings",
    )
    op.drop_table("asset_store_recovery_bindings")
