"""Add sealed authenticity completion and harden approved associations."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision: str = "0010_authenticity_completion"
down_revision: str | None = "0009_customer_image_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _assert_no_legacy_history() -> None:
    review_count = (
        op.get_bind().execute(text("SELECT count(*) FROM image_reviews")).scalar_one()
    )
    association_count = 0
    if "approved_image_associations" in _table_names():
        association_count = (
            op.get_bind()
            .execute(text("SELECT count(*) FROM approved_image_associations"))
            .scalar_one()
        )
    if review_count or association_count:
        raise RuntimeError(
            "cannot upgrade 0010 while legacy image reviews or associations "
            "lack durable authenticity completion provenance"
        )


def _create_supporting_unique_indexes() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_product_assets_customer_id_product "
        "ON product_assets (customer_id, id, product_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_image_reviews_customer_id_candidate "
        "ON image_reviews (customer_id, id, image_candidate_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_image_candidates_customer_id_job "
        "ON image_candidates (customer_id, id, output_job_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_product_asset_links_customer_link_asset_job "
        "ON product_asset_links "
        "(customer_id, id, product_asset_id, output_job_id)"
    )


def _create_review_decision_trigger() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_image_reviews_decision_insert")
    op.execute(
        "CREATE TRIGGER trg_image_reviews_decision_insert "
        "BEFORE INSERT ON image_reviews "
        "WHEN NEW.decision NOT IN "
        "('approved', 'not_my_style', 'product_detail_inaccurate', 'rejected') "
        "BEGIN SELECT RAISE(ABORT, 'invalid_image_review_decision'); END"
    )


def _create_association_triggers() -> None:
    for trigger in (
        "trg_approved_image_associations_no_update",
        "trg_approved_image_associations_no_delete",
        "trg_approved_image_associations_approved_review_only",
        "trg_approved_image_associations_source_job_only",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_no_update "
        "BEFORE UPDATE ON approved_image_associations "
        "BEGIN SELECT RAISE(ABORT, 'approved_image_association_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_no_delete "
        "BEFORE DELETE ON approved_image_associations "
        "BEGIN SELECT RAISE(ABORT, 'approved_image_association_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_approved_review_only "
        "BEFORE INSERT ON approved_image_associations "
        "WHEN NOT EXISTS (SELECT 1 FROM image_reviews AS review "
        "WHERE review.customer_id = NEW.customer_id "
        "AND review.id = NEW.image_review_id "
        "AND review.image_candidate_id = NEW.image_candidate_id "
        "AND review.decision = 'approved') "
        "BEGIN SELECT RAISE(ABORT, 'association_requires_approved_review'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_source_job_only "
        "BEFORE INSERT ON approved_image_associations "
        "WHEN NOT EXISTS (SELECT 1 FROM image_candidates AS candidate "
        "JOIN product_asset_links AS source_link "
        "ON source_link.customer_id = candidate.customer_id "
        "AND source_link.output_job_id = candidate.output_job_id "
        "JOIN product_assets AS source_asset "
        "ON source_asset.customer_id = source_link.customer_id "
        "AND source_asset.id = source_link.product_asset_id "
        "WHERE candidate.customer_id = NEW.customer_id "
        "AND candidate.id = NEW.image_candidate_id "
        "AND candidate.output_job_id = NEW.output_job_id "
        "AND source_link.id = NEW.product_asset_link_id "
        "AND source_link.product_asset_id = NEW.source_asset_id "
        "AND source_asset.product_id = NEW.product_id) "
        "BEGIN SELECT RAISE(ABORT, 'association_source_not_job_source'); END"
    )


def _create_published_association_triggers() -> None:
    for trigger in (
        "trg_approved_image_associations_source_job_only",
        "trg_approved_image_associations_no_update",
        "trg_approved_image_associations_no_delete",
        "trg_approved_image_associations_approved_review_only",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_no_update "
        "BEFORE UPDATE ON approved_image_associations "
        "BEGIN SELECT RAISE(ABORT, 'approved_image_association_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_no_delete "
        "BEFORE DELETE ON approved_image_associations "
        "BEGIN SELECT RAISE(ABORT, 'approved_image_association_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_approved_image_associations_approved_review_only "
        "BEFORE INSERT ON approved_image_associations "
        "WHEN NOT EXISTS (SELECT 1 FROM image_reviews AS review "
        "WHERE review.customer_id = NEW.customer_id "
        "AND review.id = NEW.image_review_id "
        "AND review.image_candidate_id = NEW.image_candidate_id "
        "AND review.decision = 'approved') "
        "BEGIN SELECT RAISE(ABORT, 'association_requires_approved_review'); END"
    )


def _create_authenticity_completion_triggers() -> None:
    for trigger in (
        "trg_authenticity_completions_no_update",
        "trg_authenticity_completions_no_delete",
        "trg_authenticity_completions_complete_batch_only",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(
        "CREATE TRIGGER trg_authenticity_completions_no_update "
        "BEFORE UPDATE ON authenticity_completions "
        "BEGIN SELECT RAISE(ABORT, 'authenticity_completion_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_authenticity_completions_no_delete "
        "BEFORE DELETE ON authenticity_completions "
        "BEGIN SELECT RAISE(ABORT, 'authenticity_completion_immutable'); END"
    )
    required_types = (
        "'subject_match','component_count','color_shift','artifact_scan','dimensions'"
    )
    op.execute(
        "CREATE TRIGGER trg_authenticity_completions_complete_batch_only "
        "BEFORE INSERT ON authenticity_completions "
        "WHEN NEW.checker_version != 'deterministic-local-authenticity-rules-v1' "
        "OR NEW.check_mode != 'synthetic_local_rule_based' "
        "OR NEW.previous_status != 'machine_check' "
        "OR NOT EXISTS (SELECT 1 FROM image_output_jobs AS job "
        "WHERE job.customer_id = NEW.customer_id AND job.id = NEW.output_job_id "
        "AND job.status = NEW.result_status) "
        "OR (SELECT count(*) FROM image_candidates AS candidate "
        "WHERE candidate.customer_id = NEW.customer_id "
        "AND candidate.output_job_id = NEW.output_job_id) != 3 "
        "OR (SELECT count(*) FROM authenticity_checks AS check_row "
        "JOIN image_candidates AS candidate "
        "ON candidate.customer_id = check_row.customer_id "
        "AND candidate.id = check_row.image_candidate_id "
        "WHERE candidate.customer_id = NEW.customer_id "
        "AND candidate.output_job_id = NEW.output_job_id "
        "AND check_row.checker_version = NEW.checker_version "
        "AND check_row.attempt = 1 "
        f"AND check_row.check_type IN ({required_types})) != 15 "
        "OR EXISTS (SELECT 1 FROM image_candidates AS candidate "
        "WHERE candidate.customer_id = NEW.customer_id "
        "AND candidate.output_job_id = NEW.output_job_id "
        "AND (SELECT count(*) FROM authenticity_checks AS check_row "
        "WHERE check_row.customer_id = candidate.customer_id "
        "AND check_row.image_candidate_id = candidate.id "
        "AND check_row.checker_version = NEW.checker_version "
        "AND check_row.attempt = 1 "
        f"AND check_row.check_type IN ({required_types})) != 5) "
        "OR (NEW.result_status = 'awaiting_customer_review' AND EXISTS "
        "(SELECT 1 FROM authenticity_checks AS check_row "
        "JOIN image_candidates AS candidate "
        "ON candidate.customer_id = check_row.customer_id "
        "AND candidate.id = check_row.image_candidate_id "
        "WHERE candidate.customer_id = NEW.customer_id "
        "AND candidate.output_job_id = NEW.output_job_id "
        "AND check_row.passed = 0)) "
        "OR (NEW.result_status = 'authenticity_failed' AND NOT EXISTS "
        "(SELECT 1 FROM authenticity_checks AS check_row "
        "JOIN image_candidates AS candidate "
        "ON candidate.customer_id = check_row.customer_id "
        "AND candidate.id = check_row.image_candidate_id "
        "WHERE candidate.customer_id = NEW.customer_id "
        "AND candidate.output_job_id = NEW.output_job_id "
        "AND check_row.passed = 0)) "
        "BEGIN SELECT RAISE(ABORT, 'authenticity_completion_requires_batch'); END"
    )


def _drop_empty_incomplete_table(name: str, expected_columns: set[str]) -> None:
    if name not in _table_names():
        return
    existing = {
        str(column["name"]) for column in inspect(op.get_bind()).get_columns(name)
    }
    if expected_columns <= existing:
        return
    count = op.get_bind().execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
    if count:
        raise RuntimeError(f"cannot repair incomplete nonempty 0010 table: {name}")
    op.drop_table(name)


def _create_published_association_table() -> None:
    op.create_table(
        "approved_image_associations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("image_review_id", sa.String(length=36), nullable=False),
        sa.Column("image_candidate_id", sa.String(length=36), nullable=False),
        sa.Column("source_asset_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=255), nullable=False),
        sa.Column("image_kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "image_kind IN ('real_photo', 'style_image')",
            name="ck_approved_image_associations_image_kind",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(
            ["customer_id", "image_candidate_id"],
            ["image_candidates.customer_id", "image_candidates.id"],
            name="fk_approved_image_associations_customer_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "source_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_approved_image_associations_customer_source_product",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "image_review_id", "image_candidate_id"],
            [
                "image_reviews.customer_id",
                "image_reviews.id",
                "image_reviews.image_candidate_id",
            ],
            name="fk_approved_image_associations_customer_review_candidate",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "image_review_id",
            "source_asset_id",
            name="uq_approved_image_associations_review_source",
        ),
    )


def _create_published_association_indexes() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_approved_image_associations_candidate_source "
        "ON approved_image_associations (image_candidate_id, source_asset_id)"
    )
    for name, columns in (
        ("ix_approved_image_associations_customer_id", "customer_id"),
        ("ix_approved_image_associations_image_review_id", "image_review_id"),
        ("ix_approved_image_associations_image_candidate_id", "image_candidate_id"),
        ("ix_approved_image_associations_source_asset_id", "source_asset_id"),
        ("ix_approved_image_associations_product_id", "product_id"),
    ):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON approved_image_associations ({columns})"
        )


def upgrade() -> None:
    """Create one immutable association row per approved candidate/source pair."""
    # This guard must precede every DDL statement because SQLite DDL is not
    # transactional. An old review cannot be safely inferred or backfilled.
    _assert_no_legacy_history()
    _create_supporting_unique_indexes()
    _create_review_decision_trigger()
    _drop_empty_incomplete_table(
        "authenticity_completions",
        {
            "id",
            "customer_id",
            "output_job_id",
            "checker_version",
            "check_mode",
            "candidate_batch_digest",
            "previous_status",
            "result_status",
            "created_at",
        },
    )
    if "authenticity_completions" not in _table_names():
        op.create_table(
            "authenticity_completions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("customer_id", sa.String(length=36), nullable=False),
            sa.Column("output_job_id", sa.String(length=36), nullable=False),
            sa.Column("checker_version", sa.String(length=100), nullable=False),
            sa.Column("check_mode", sa.String(length=100), nullable=False),
            sa.Column("candidate_batch_digest", sa.String(length=64), nullable=False),
            sa.Column("previous_status", sa.String(length=64), nullable=False),
            sa.Column("result_status", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "length(candidate_batch_digest) = 64",
                name="ck_authenticity_completions_digest_length",
            ),
            sa.CheckConstraint(
                "result_status IN ('awaiting_customer_review', 'authenticity_failed')",
                name="ck_authenticity_completions_result_status",
            ),
            sa.CheckConstraint(
                "previous_status = 'machine_check'",
                name="ck_authenticity_completions_previous_status",
            ),
            sa.CheckConstraint(
                "checker_version = 'deterministic-local-authenticity-rules-v1'",
                name="ck_authenticity_completions_checker_version",
            ),
            sa.CheckConstraint(
                "check_mode = 'synthetic_local_rule_based'",
                name="ck_authenticity_completions_check_mode",
            ),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
            sa.ForeignKeyConstraint(
                ["customer_id", "output_job_id"],
                ["image_output_jobs.customer_id", "image_output_jobs.id"],
                name="fk_authenticity_completions_customer_job",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "output_job_id", name="uq_authenticity_completions_output_job"
            ),
        )
    for name, columns in (
        ("ix_authenticity_completions_customer_id", "customer_id"),
        ("ix_authenticity_completions_output_job_id", "output_job_id"),
    ):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON authenticity_completions ({columns})"
        )
    _create_authenticity_completion_triggers()
    _drop_empty_incomplete_table(
        "approved_image_associations",
        {
            "id",
            "customer_id",
            "image_review_id",
            "image_candidate_id",
            "output_job_id",
            "product_asset_link_id",
            "source_asset_id",
            "product_id",
            "image_kind",
            "created_at",
        },
    )
    if "approved_image_associations" not in _table_names():
        op.create_table(
            "approved_image_associations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("customer_id", sa.String(length=36), nullable=False),
            sa.Column("image_review_id", sa.String(length=36), nullable=False),
            sa.Column("image_candidate_id", sa.String(length=36), nullable=False),
            sa.Column("output_job_id", sa.String(length=36), nullable=False),
            sa.Column("product_asset_link_id", sa.String(length=36), nullable=False),
            sa.Column("source_asset_id", sa.String(length=36), nullable=False),
            sa.Column("product_id", sa.String(length=255), nullable=False),
            sa.Column("image_kind", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "image_kind IN ('real_photo', 'style_image')",
                name="ck_approved_image_associations_image_kind",
            ),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
            sa.ForeignKeyConstraint(
                ["customer_id", "image_candidate_id", "output_job_id"],
                [
                    "image_candidates.customer_id",
                    "image_candidates.id",
                    "image_candidates.output_job_id",
                ],
                name="fk_approved_image_associations_customer_candidate_job",
            ),
            sa.ForeignKeyConstraint(
                [
                    "customer_id",
                    "product_asset_link_id",
                    "source_asset_id",
                    "output_job_id",
                ],
                [
                    "product_asset_links.customer_id",
                    "product_asset_links.id",
                    "product_asset_links.product_asset_id",
                    "product_asset_links.output_job_id",
                ],
                name="fk_approved_image_associations_customer_source_link_job",
            ),
            sa.ForeignKeyConstraint(
                ["customer_id", "source_asset_id", "product_id"],
                [
                    "product_assets.customer_id",
                    "product_assets.id",
                    "product_assets.product_id",
                ],
                name="fk_approved_image_associations_customer_source_product",
            ),
            sa.ForeignKeyConstraint(
                ["customer_id", "image_review_id", "image_candidate_id"],
                [
                    "image_reviews.customer_id",
                    "image_reviews.id",
                    "image_reviews.image_candidate_id",
                ],
                name="fk_approved_image_associations_customer_review_candidate",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "image_review_id",
                "source_asset_id",
                name="uq_approved_image_associations_review_source",
            ),
        )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_approved_image_associations_candidate_source "
        "ON approved_image_associations (image_candidate_id, source_asset_id)"
    )
    for name, columns in (
        ("ix_approved_image_associations_customer_id", "customer_id"),
        ("ix_approved_image_associations_image_review_id", "image_review_id"),
        ("ix_approved_image_associations_image_candidate_id", "image_candidate_id"),
        ("ix_approved_image_associations_output_job_id", "output_job_id"),
        (
            "ix_approved_image_associations_product_asset_link_id",
            "product_asset_link_id",
        ),
        ("ix_approved_image_associations_source_asset_id", "source_asset_id"),
        ("ix_approved_image_associations_product_id", "product_id"),
    ):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON approved_image_associations ({columns})"
        )
    _create_association_triggers()


def downgrade() -> None:
    """Restore published 0009 only when no 0010 durable history would be lost."""
    # Both guards precede every DDL statement so a failed downgrade leaves the
    # revision and schema untouched on non-transactional SQLite.
    if "authenticity_completions" in _table_names():
        count = (
            op.get_bind()
            .execute(text("SELECT count(*) FROM authenticity_completions"))
            .scalar_one()
        )
        if count:
            raise RuntimeError(
                "cannot discard durable authenticity completions while rows exist"
            )
    if "approved_image_associations" in _table_names():
        count = (
            op.get_bind()
            .execute(text("SELECT count(*) FROM approved_image_associations"))
            .scalar_one()
        )
        if count:
            raise RuntimeError(
                "cannot discard customer-approved image associations while rows exist"
            )

    if "approved_image_associations" in _table_names():
        for trigger in (
            "trg_approved_image_associations_source_job_only",
            "trg_approved_image_associations_approved_review_only",
            "trg_approved_image_associations_no_delete",
            "trg_approved_image_associations_no_update",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        op.drop_table("approved_image_associations")
    if "authenticity_completions" in _table_names():
        for trigger in (
            "trg_authenticity_completions_complete_batch_only",
            "trg_authenticity_completions_no_delete",
            "trg_authenticity_completions_no_update",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        op.drop_table("authenticity_completions")
    op.execute("DROP INDEX IF EXISTS uq_product_asset_links_customer_link_asset_job")
    op.execute("DROP INDEX IF EXISTS uq_image_candidates_customer_id_job")
    _create_published_association_table()
    _create_published_association_indexes()
    _create_published_association_triggers()
    _create_review_decision_trigger()
