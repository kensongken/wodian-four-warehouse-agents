"""Persist reusable product identity observations and human reviews."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_product_identity_observations"
down_revision: str | None = "0037_foreground_benchmark_shadow_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_product_identity_cards_reusable_scope",
        "product_identity_card_versions",
        ["customer_id", "id", "product_id", "card_sha256"],
        unique=True,
    )
    op.create_table(
        "product_identity_observation_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("product_identity_card_version_id", sa.String(36), nullable=False),
        sa.Column("product_identity_card_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("observer", sa.String(100), nullable=False),
        sa.Column("observer_version", sa.String(100), nullable=False),
        sa.Column("observation_mode", sa.String(100), nullable=False),
        sa.Column("policy_version", sa.String(100), nullable=False),
        sa.Column("authorized_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("dispatch_state", sa.String(32), nullable=False),
        sa.Column("outbound_attempts", sa.Integer(), nullable=False),
        sa.Column("observation_payload", sa.JSON()),
        sa.Column("decision", sa.String(16)),
        sa.Column("reason_codes", sa.JSON()),
        sa.Column("result_sha256", sa.String(64)),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "product_asset_id", "product_id"],
            ["product_assets.customer_id", "product_assets.id", "product_assets.product_id"],
            name="fk_product_identity_observation_asset",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "product_identity_card_version_id", "product_id", "product_identity_card_sha256"],
            ["product_identity_card_versions.customer_id", "product_identity_card_versions.id", "product_identity_card_versions.product_id", "product_identity_card_versions.card_sha256"],
            name="fk_product_identity_observation_card",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "id", name="uq_product_identity_observation_customer_id"),
        sa.UniqueConstraint(
            "customer_id", "id", "product_asset_id", "product_id", "source_content_sha256",
            "product_identity_card_version_id", "product_identity_card_sha256", "result_sha256",
            name="uq_product_identity_observation_evidence_scope",
        ),
        sa.UniqueConstraint("customer_id", "idempotency_key", name="uq_product_identity_observation_customer_key"),
        sa.CheckConstraint(
            "length(source_content_sha256) = 64 AND length(product_identity_card_sha256) = 64 "
            "AND length(input_manifest_sha256) = 64 AND "
            "(result_sha256 IS NULL OR length(result_sha256) = 64)",
            name="ck_product_identity_observation_digests",
        ),
        sa.CheckConstraint("authorized_usd > 0", name="ck_product_identity_observation_usd_positive"),
        sa.CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_product_identity_observation_dispatch_state",
        ),
        sa.CheckConstraint("outbound_attempts IN (0, 1)", name="ck_product_identity_observation_one_attempt"),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('pass', 'needs_review', 'block')",
            name="ck_product_identity_observation_decision",
        ),
    )
    op.create_index(
        "ix_product_identity_observation_asset_card",
        "product_identity_observation_runs",
        ["customer_id", "product_asset_id", "product_identity_card_version_id"],
    )
    op.create_table(
        "product_identity_observation_reviews",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("observation_run_id", sa.String(36), nullable=False),
        sa.Column("product_asset_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("product_identity_card_version_id", sa.String(36), nullable=False),
        sa.Column("product_identity_card_sha256", sa.String(64), nullable=False),
        sa.Column("observation_result_sha256", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("reviewed_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "observation_run_id", "product_asset_id", "product_id", "source_content_sha256",
             "product_identity_card_version_id", "product_identity_card_sha256", "observation_result_sha256"],
            ["product_identity_observation_runs.customer_id", "product_identity_observation_runs.id",
             "product_identity_observation_runs.product_asset_id", "product_identity_observation_runs.product_id",
             "product_identity_observation_runs.source_content_sha256",
             "product_identity_observation_runs.product_identity_card_version_id",
             "product_identity_observation_runs.product_identity_card_sha256",
             "product_identity_observation_runs.result_sha256"],
            name="fk_product_identity_observation_review_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "id", name="uq_product_identity_observation_reviews_customer_id"),
        sa.UniqueConstraint("observation_run_id", name="uq_product_identity_observation_review_run"),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="ck_product_identity_observation_review_decision"),
        sa.CheckConstraint("reviewed_by = 'local_customer'", name="ck_product_identity_observation_review_reviewer"),
        sa.CheckConstraint("length(trim(reason)) BETWEEN 1 AND 500", name="ck_product_identity_observation_review_reason"),
    )
    for name, type_ in (
        ("product_identity_observation_run_id", sa.String(36)),
        ("product_identity_observation_result_sha256", sa.String(64)),
        ("product_identity_observation_review_id", sa.String(36)),
    ):
        op.add_column("style_suite_runs", sa.Column(name, type_))
    op.execute(
        "CREATE TRIGGER trg_product_identity_observation_scope BEFORE INSERT ON product_identity_observation_runs "
        "WHEN NOT EXISTS (SELECT 1 FROM product_assets a WHERE a.customer_id = NEW.customer_id "
        "AND a.id = NEW.product_asset_id AND a.product_id = NEW.product_id "
        "AND a.content_sha256 = NEW.source_content_sha256 AND a.confirmed = 1 "
        "AND a.identity = 'original_real_photo') "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_observation_scope_mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_observation_immutable BEFORE UPDATE ON product_identity_observation_runs "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_observation_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_observation_no_delete BEFORE DELETE ON product_identity_observation_runs "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_observation_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_observation_review_nonpassing BEFORE INSERT ON product_identity_observation_reviews "
        "WHEN NOT EXISTS (SELECT 1 FROM product_identity_observation_runs r WHERE r.customer_id = NEW.customer_id "
        "AND r.id = NEW.observation_run_id AND r.decision IN ('needs_review', 'block')) "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_observation_review_invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_observation_review_immutable BEFORE UPDATE ON product_identity_observation_reviews "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_observation_review_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_product_identity_observation_review_no_delete BEFORE DELETE ON product_identity_observation_reviews "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_observation_review_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_identity_observation_complete BEFORE INSERT ON style_suite_runs "
        "WHEN (NEW.product_identity_observation_run_id IS NULL) != "
        "(NEW.product_identity_observation_result_sha256 IS NULL) "
        "OR (NEW.product_identity_observation_review_id IS NOT NULL "
        "AND NEW.product_identity_observation_run_id IS NULL) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_identity_observation_incomplete'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_identity_observation_scope BEFORE INSERT ON style_suite_runs "
        "WHEN NEW.product_identity_observation_run_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM product_identity_observation_runs r WHERE r.customer_id = NEW.customer_id "
        "AND r.id = NEW.product_identity_observation_run_id AND r.product_asset_id = NEW.source_asset_id "
        "AND r.product_id = NEW.product_id AND r.source_content_sha256 = NEW.source_content_sha256 "
        "AND r.product_identity_card_version_id = NEW.product_identity_card_version_id "
        "AND r.product_identity_card_sha256 = NEW.product_identity_card_sha256 "
        "AND r.result_sha256 = NEW.product_identity_observation_result_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_identity_observation_mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_identity_observation_review_scope BEFORE INSERT ON style_suite_runs "
        "WHEN NEW.product_identity_observation_review_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM product_identity_observation_reviews r WHERE r.customer_id = NEW.customer_id "
        "AND r.id = NEW.product_identity_observation_review_id "
        "AND r.observation_run_id = NEW.product_identity_observation_run_id "
        "AND r.observation_result_sha256 = NEW.product_identity_observation_result_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_identity_observation_review_mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_style_suite_identity_observation_immutable BEFORE UPDATE ON style_suite_runs WHEN "
        "OLD.product_identity_observation_run_id IS NOT NEW.product_identity_observation_run_id OR "
        "OLD.product_identity_observation_result_sha256 IS NOT NEW.product_identity_observation_result_sha256 OR "
        "OLD.product_identity_observation_review_id IS NOT NEW.product_identity_observation_review_id "
        "BEGIN SELECT RAISE(ABORT, 'style_suite_run_immutable'); END"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("product_identity_observation_reviews", "product_identity_observation_runs"):
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one():
            raise RuntimeError("cannot discard product identity observation evidence")
    for trigger in (
        "trg_style_suite_identity_observation_immutable",
        "trg_style_suite_identity_observation_review_scope",
        "trg_style_suite_identity_observation_scope",
        "trg_style_suite_identity_observation_complete",
        "trg_product_identity_observation_review_no_delete",
        "trg_product_identity_observation_review_immutable",
        "trg_product_identity_observation_review_nonpassing",
        "trg_product_identity_observation_no_delete",
        "trg_product_identity_observation_immutable",
        "trg_product_identity_observation_scope",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for name in (
        "product_identity_observation_review_id",
        "product_identity_observation_result_sha256",
        "product_identity_observation_run_id",
    ):
        op.drop_column("style_suite_runs", name)
    op.drop_table("product_identity_observation_reviews")
    op.drop_index("ix_product_identity_observation_asset_card", table_name="product_identity_observation_runs")
    op.drop_table("product_identity_observation_runs")
    op.drop_index("uq_product_identity_cards_reusable_scope", table_name="product_identity_card_versions")
