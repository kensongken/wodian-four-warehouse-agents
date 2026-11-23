"""enforce cleanup claim and upload generation invariants

Revision ID: 0008_cleanup_state_invariants
Revises: 0007_cleanup_job_claims
Create Date: 2026-08-14
"""

from alembic import op

revision = "0008_cleanup_state_invariants"
down_revision = "0007_cleanup_job_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_storage_cleanup_jobs_storage_cleanup_jobs_attempts_n_f2ed"),
        "storage_cleanup_jobs",
        "attempts >= 0",
    )
    op.create_check_constraint(
        op.f("ck_storage_cleanup_jobs_storage_cleanup_jobs_generation_a180"),
        "storage_cleanup_jobs",
        "claim_generation >= 0",
    )
    op.create_check_constraint(
        op.f("ck_storage_cleanup_jobs_storage_cleanup_jobs_claim_stat_de7e"),
        "storage_cleanup_jobs",
        "(status = 'processing' AND claim_id IS NOT NULL AND claimed_at IS NOT NULL AND terminal_code IS NULL) OR "
        "(status = 'pending' AND claim_id IS NULL AND claimed_at IS NULL AND terminal_code IS NULL) OR "
        "(status = 'dead' AND claim_id IS NULL AND claimed_at IS NULL AND terminal_code IS NOT NULL)",
    )
    op.create_check_constraint(
        "initialization_generation_nonnegative",
        "stored_blobs",
        "initialization_generation >= 0",
    )
    op.create_check_constraint(
        "completion_generation_nonnegative",
        "stored_blobs",
        "completion_generation >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "completion_generation_nonnegative", "stored_blobs", type_="check"
    )
    op.drop_constraint(
        "initialization_generation_nonnegative", "stored_blobs", type_="check"
    )
    op.drop_constraint(
        op.f("ck_storage_cleanup_jobs_storage_cleanup_jobs_claim_stat_de7e"),
        "storage_cleanup_jobs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_storage_cleanup_jobs_storage_cleanup_jobs_generation_a180"),
        "storage_cleanup_jobs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_storage_cleanup_jobs_storage_cleanup_jobs_attempts_n_f2ed"),
        "storage_cleanup_jobs",
        type_="check",
    )
