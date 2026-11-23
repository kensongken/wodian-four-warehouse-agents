"""Persist product identity analysis ledgers and confirmed card versions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_product_identity_cards"
down_revision: str | None = "0029_fidelity_warning_acknowledgement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_identity_analysis_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("authorized_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("dispatch_state", sa.String(32), nullable=False),
        sa.Column("outbound_attempts", sa.Integer(), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "product_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_product_identity_analysis_customer_asset_product",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_product_identity_analysis_customer_id"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "id",
            "product_asset_id",
            "product_id",
            "source_content_sha256",
            name="uq_product_identity_analysis_source_lineage",
        ),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_product_identity_analysis_customer_key",
        ),
        sa.CheckConstraint(
            "length(source_content_sha256) = 64",
            name="ck_product_identity_analysis_source_sha256",
        ),
        sa.CheckConstraint(
            "length(input_manifest_sha256) = 64",
            name="ck_product_identity_analysis_manifest_sha256",
        ),
        sa.CheckConstraint(
            "authorized_usd > 0", name="ck_product_identity_analysis_usd_positive"
        ),
        sa.CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_product_identity_analysis_dispatch_state",
        ),
        sa.CheckConstraint(
            "outbound_attempts IN (0, 1)",
            name="ck_product_identity_analysis_one_attempt",
        ),
    )
    op.create_index(
        "ix_product_identity_analysis_customer",
        "product_identity_analysis_runs",
        ["customer_id"],
    )
    op.create_index(
        "ix_product_identity_analysis_customer_asset",
        "product_identity_analysis_runs",
        ["customer_id", "product_asset_id"],
    )

    op.create_table(
        "product_identity_card_versions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("analysis_run_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("card_payload", sa.JSON(), nullable=False),
        sa.Column("card_sha256", sa.String(64), nullable=False),
        sa.Column("confirmed_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "product_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_product_identity_cards_customer_asset_product",
        ),
        sa.ForeignKeyConstraint(
            [
                "customer_id",
                "analysis_run_id",
                "product_asset_id",
                "product_id",
                "source_content_sha256",
            ],
            [
                "product_identity_analysis_runs.customer_id",
                "product_identity_analysis_runs.id",
                "product_identity_analysis_runs.product_asset_id",
                "product_identity_analysis_runs.product_id",
                "product_identity_analysis_runs.source_content_sha256",
            ],
            name="fk_product_identity_cards_analysis_lineage",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_product_identity_cards_customer_id"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "product_asset_id",
            "version",
            name="uq_product_identity_cards_customer_asset_version",
        ),
        sa.UniqueConstraint(
            "analysis_run_id",
            "card_sha256",
            name="uq_product_identity_cards_analysis_hash",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_product_identity_cards_version_positive"
        ),
        sa.CheckConstraint(
            "length(source_content_sha256) = 64",
            name="ck_product_identity_cards_source_sha256",
        ),
        sa.CheckConstraint(
            "length(card_sha256) = 64", name="ck_product_identity_cards_sha256"
        ),
        sa.CheckConstraint(
            "confirmed_by = 'local_customer'",
            name="ck_product_identity_cards_confirmer",
        ),
    )
    op.create_index(
        "ix_product_identity_cards_customer_asset",
        "product_identity_card_versions",
        ["customer_id", "product_asset_id"],
    )
    op.create_index(
        "ix_product_identity_cards_analysis",
        "product_identity_card_versions",
        ["analysis_run_id"],
    )
    _install_immutability_guards()


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.execute(
            sa.text("SELECT count(*) FROM product_identity_card_versions")
        ).scalar_one()
        or bind.execute(
            sa.text("SELECT count(*) FROM product_identity_analysis_runs")
        ).scalar_one()
    ):
        raise RuntimeError("cannot discard product identity evidence")
    op.execute("DROP TRIGGER IF EXISTS trg_product_identity_card_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_product_identity_card_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_product_identity_analysis_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_product_identity_analysis_scope_immutable")
    op.drop_index(
        "ix_product_identity_cards_analysis",
        table_name="product_identity_card_versions",
    )
    op.drop_index(
        "ix_product_identity_cards_customer_asset",
        table_name="product_identity_card_versions",
    )
    op.drop_table("product_identity_card_versions")
    op.drop_index(
        "ix_product_identity_analysis_customer_asset",
        table_name="product_identity_analysis_runs",
    )
    op.drop_index(
        "ix_product_identity_analysis_customer",
        table_name="product_identity_analysis_runs",
    )
    op.drop_table("product_identity_analysis_runs")


def _install_immutability_guards() -> None:
    immutable_analysis_columns = (
        "id",
        "customer_id",
        "product_asset_id",
        "product_id",
        "source_content_sha256",
        "idempotency_key",
        "model",
        "authorized_usd",
        "input_manifest_sha256",
        "input_manifest",
        "created_at",
    )
    condition = " OR ".join(
        f"OLD.{column} IS NOT NEW.{column}" for column in immutable_analysis_columns
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_analysis_scope_immutable "
        "BEFORE UPDATE ON product_identity_analysis_runs "
        f"WHEN {condition} "
        "BEGIN SELECT RAISE(ABORT, "
        "'product_identity_analysis_authorization_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_analysis_no_delete "
        "BEFORE DELETE ON product_identity_analysis_runs "
        "BEGIN SELECT RAISE(ABORT, "
        "'product_identity_analysis_authorization_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_card_immutable "
        "BEFORE UPDATE ON product_identity_card_versions "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_card_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_card_no_delete "
        "BEFORE DELETE ON product_identity_card_versions "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_card_immutable'); END"
    )
