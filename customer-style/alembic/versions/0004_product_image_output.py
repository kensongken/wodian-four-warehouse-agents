"""Add customer-isolated product assets and immutable image output history."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_product_image_output"
down_revision: str | None = "0003_style_card_sample_links_immutable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_immutable_triggers(table_name: str, error_code: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table_name}_no_update "
        f"BEFORE UPDATE ON {table_name} "
        f"BEGIN SELECT RAISE(ABORT, '{error_code}'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table_name}_no_delete "
        f"BEFORE DELETE ON {table_name} "
        f"BEGIN SELECT RAISE(ABORT, '{error_code}'); END"
    )


def _drop_immutable_triggers(table_name: str) -> None:
    op.execute(f"DROP TRIGGER trg_{table_name}_no_delete")
    op.execute(f"DROP TRIGGER trg_{table_name}_no_update")


def _create_image_output_job_triggers() -> None:
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_provenance_no_update "
        "BEFORE UPDATE ON image_output_jobs "
        "WHEN NEW.id IS NOT OLD.id "
        "OR NEW.customer_id IS NOT OLD.customer_id "
        "OR NEW.output_type IS NOT OLD.output_type "
        "OR NEW.style_card_version_id IS NOT OLD.style_card_version_id "
        "OR NEW.scene_style_id IS NOT OLD.scene_style_id "
        "OR NEW.output_ratio IS NOT OLD.output_ratio "
        "OR NEW.adapter_version IS NOT OLD.adapter_version "
        "OR NEW.request_metadata IS NOT OLD.request_metadata "
        "OR NEW.idempotency_key IS NOT OLD.idempotency_key "
        "OR NEW.created_at IS NOT OLD.created_at "
        "BEGIN SELECT RAISE(ABORT, 'image_output_job_provenance_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_status_transition "
        "BEFORE UPDATE OF status ON image_output_jobs "
        "WHEN NEW.status IS NOT OLD.status AND NOT ("
        "(OLD.status = 'queued' AND NEW.status IN "
        "('generating', 'insufficient_information', 'permission_unknown', 'disabled')) "
        "OR (OLD.status = 'generating' AND NEW.status IN "
        "('machine_check', 'generation_failed', 'disabled')) "
        "OR (OLD.status = 'machine_check' AND NEW.status IN "
        "('awaiting_customer_review', 'authenticity_failed', 'disabled')) "
        "OR (OLD.status = 'awaiting_customer_review' AND NEW.status IN "
        "('customer_approved', 'customer_rejected', 'disabled'))"
        ") BEGIN SELECT RAISE(ABORT, "
        "'invalid_image_output_job_status_transition'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_image_output_jobs_no_delete "
        "BEFORE DELETE ON image_output_jobs "
        "BEGIN SELECT RAISE(ABORT, 'image_output_job_provenance_immutable'); END"
    )


def _drop_image_output_job_triggers() -> None:
    op.execute("DROP TRIGGER trg_image_output_jobs_no_delete")
    op.execute("DROP TRIGGER trg_image_output_jobs_status_transition")
    op.execute("DROP TRIGGER trg_image_output_jobs_provenance_no_update")


def upgrade() -> None:
    """Create product image persistence with customer-scoped foreign keys."""
    with op.batch_alter_table("scene_styles") as batch_op:
        batch_op.create_unique_constraint(
            "uq_scene_styles_customer_id_id", ["customer_id", "id"]
        )

    op.create_table(
        "product_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=255), nullable=False),
        sa.Column("photo_id", sa.String(length=255), nullable=False),
        sa.Column("identity", sa.String(length=64), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("quality_flags", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "height > 0", name="ck_product_assets_height_positive"
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64", name="ck_product_assets_sha256_length"
        ),
        sa.CheckConstraint("width > 0", name="ck_product_assets_width_positive"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_product_assets_customer_id_id"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "photo_id",
            "content_sha256",
            name="uq_product_assets_customer_photo_hash",
        ),
    )
    op.create_index("ix_product_assets_customer_id", "product_assets", ["customer_id"])
    op.create_index("ix_product_assets_product_id", "product_assets", ["product_id"])

    op.create_table(
        "product_asset_permission_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("product_asset_id", sa.String(length=36), nullable=False),
        sa.Column("transform_permission", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(provenance)) > 0",
            name="ck_product_asset_permission_events_provenance",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "product_asset_id"],
            ["product_assets.customer_id", "product_assets.id"],
            name="fk_product_asset_permission_events_customer_asset",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id",
            "id",
            name="uq_product_asset_permission_events_customer_id_id",
        ),
    )
    op.create_index(
        "ix_product_asset_permission_events_latest",
        "product_asset_permission_events",
        ["customer_id", "product_asset_id", "occurred_at"],
    )

    op.create_table(
        "image_output_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("output_type", sa.String(length=32), nullable=False),
        sa.Column("style_card_version_id", sa.String(length=36), nullable=True),
        sa.Column("scene_style_id", sa.String(length=36), nullable=True),
        sa.Column("output_ratio", sa.String(length=20), nullable=False),
        sa.Column("adapter_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("request_metadata", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "scene_style_id"],
            ["scene_styles.customer_id", "scene_styles.id"],
            name="fk_image_output_jobs_customer_scene_style",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "style_card_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_image_output_jobs_customer_style_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_image_output_jobs_customer_id_id"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_image_output_jobs_customer_idempotency",
        ),
    )
    op.create_index(
        "ix_image_output_jobs_customer_id", "image_output_jobs", ["customer_id"]
    )
    op.create_index(
        "ix_image_output_jobs_scene_style_id", "image_output_jobs", ["scene_style_id"]
    )
    op.create_index(
        "ix_image_output_jobs_style_card_version_id",
        "image_output_jobs",
        ["style_card_version_id"],
    )

    op.create_table(
        "product_asset_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("product_asset_id", sa.String(length=36), nullable=False),
        sa.Column("output_job_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 5", name="ck_product_asset_links_position"
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "product_asset_id"],
            ["product_assets.customer_id", "product_assets.id"],
            name="fk_product_asset_links_customer_asset",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_product_asset_links_customer_job",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "output_job_id",
            "product_asset_id",
            name="uq_product_asset_links_job_asset",
        ),
        sa.UniqueConstraint(
            "output_job_id", "position", name="uq_product_asset_links_job_position"
        ),
    )
    op.create_index(
        "ix_product_asset_links_customer_id", "product_asset_links", ["customer_id"]
    )
    op.create_index(
        "ix_product_asset_links_output_job_id",
        "product_asset_links",
        ["output_job_id"],
    )
    op.create_index(
        "ix_product_asset_links_product_asset_id",
        "product_asset_links",
        ["product_asset_id"],
    )

    op.create_table(
        "image_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("output_job_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("identity", sa.String(length=64), nullable=False),
        sa.Column("artifact_path", sa.String(length=1024), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("adapter_version", sa.String(length=100), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "height > 0", name="ck_image_candidates_height_positive"
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64", name="ck_image_candidates_sha256_length"
        ),
        sa.CheckConstraint(
            "sequence_number BETWEEN 1 AND 3",
            name="ck_image_candidates_sequence_number",
        ),
        sa.CheckConstraint(
            "width > 0", name="ck_image_candidates_width_positive"
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_candidates_customer_job",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id", "id", name="uq_image_candidates_customer_id_id"
        ),
        sa.UniqueConstraint(
            "output_job_id",
            "sequence_number",
            name="uq_image_candidates_job_sequence",
        ),
    )
    op.create_index(
        "ix_image_candidates_customer_id", "image_candidates", ["customer_id"]
    )
    op.create_index(
        "ix_image_candidates_output_job_id", "image_candidates", ["output_job_id"]
    )

    op.create_table(
        "authenticity_checks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("image_candidate_id", sa.String(length=36), nullable=False),
        sa.Column("check_type", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("checker_version", sa.String(length=100), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt > 0", name="ck_authenticity_checks_attempt_positive"
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "image_candidate_id"],
            ["image_candidates.customer_id", "image_candidates.id"],
            name="fk_authenticity_checks_customer_candidate",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "image_candidate_id",
            "check_type",
            "attempt",
            name="uq_authenticity_checks_candidate_type_attempt",
        ),
    )
    op.create_index(
        "ix_authenticity_checks_customer_id", "authenticity_checks", ["customer_id"]
    )
    op.create_index(
        "ix_authenticity_checks_image_candidate_id",
        "authenticity_checks",
        ["image_candidate_id"],
    )

    op.create_table(
        "image_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("image_candidate_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "image_candidate_id"],
            ["image_candidates.customer_id", "image_candidates.id"],
            name="fk_image_reviews_customer_candidate",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "image_candidate_id", name="uq_image_reviews_candidate"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_image_reviews_customer_idempotency",
        ),
    )
    op.create_index("ix_image_reviews_customer_id", "image_reviews", ["customer_id"])
    op.create_index(
        "ix_image_reviews_image_candidate_id",
        "image_reviews",
        ["image_candidate_id"],
    )

    for table_name, error_code in (
        ("product_assets", "product_asset_immutable"),
        (
            "product_asset_permission_events",
            "product_asset_permission_event_immutable",
        ),
        ("product_asset_links", "product_asset_link_immutable"),
        ("image_candidates", "image_candidate_immutable"),
        ("authenticity_checks", "authenticity_check_immutable"),
        ("image_reviews", "image_review_immutable"),
    ):
        _create_immutable_triggers(table_name, error_code)
    _create_image_output_job_triggers()


def downgrade() -> None:
    """Remove product image persistence in reverse dependency order."""
    _drop_image_output_job_triggers()
    for table_name in (
        "image_reviews",
        "authenticity_checks",
        "image_candidates",
        "product_asset_links",
        "product_asset_permission_events",
        "product_assets",
    ):
        _drop_immutable_triggers(table_name)

    op.drop_index("ix_image_reviews_image_candidate_id", table_name="image_reviews")
    op.drop_index("ix_image_reviews_customer_id", table_name="image_reviews")
    op.drop_table("image_reviews")
    op.drop_index(
        "ix_authenticity_checks_image_candidate_id", table_name="authenticity_checks"
    )
    op.drop_index(
        "ix_authenticity_checks_customer_id", table_name="authenticity_checks"
    )
    op.drop_table("authenticity_checks")
    op.drop_index("ix_image_candidates_output_job_id", table_name="image_candidates")
    op.drop_index("ix_image_candidates_customer_id", table_name="image_candidates")
    op.drop_table("image_candidates")
    op.drop_index(
        "ix_product_asset_links_product_asset_id", table_name="product_asset_links"
    )
    op.drop_index(
        "ix_product_asset_links_output_job_id", table_name="product_asset_links"
    )
    op.drop_index(
        "ix_product_asset_links_customer_id", table_name="product_asset_links"
    )
    op.drop_table("product_asset_links")
    op.drop_index(
        "ix_image_output_jobs_style_card_version_id", table_name="image_output_jobs"
    )
    op.drop_index(
        "ix_image_output_jobs_scene_style_id", table_name="image_output_jobs"
    )
    op.drop_index("ix_image_output_jobs_customer_id", table_name="image_output_jobs")
    op.drop_table("image_output_jobs")
    op.drop_index(
        "ix_product_asset_permission_events_latest",
        table_name="product_asset_permission_events",
    )
    op.drop_table("product_asset_permission_events")
    op.drop_index("ix_product_assets_product_id", table_name="product_assets")
    op.drop_index("ix_product_assets_customer_id", table_name="product_assets")
    op.drop_table("product_assets")

    with op.batch_alter_table("scene_styles") as batch_op:
        batch_op.drop_constraint("uq_scene_styles_customer_id_id", type_="unique")
