"""Allow only the sealed product identity observation dispatch lifecycle."""

from collections.abc import Sequence

from alembic import op

revision: str = "0039_product_identity_observation_lifecycle"
down_revision: str | None = "0038_product_identity_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATIC_SCOPE_UNCHANGED = " AND ".join(
    f"OLD.{column} IS NEW.{column}"
    for column in (
        "id",
        "customer_id",
        "product_asset_id",
        "product_id",
        "source_content_sha256",
        "product_identity_card_version_id",
        "product_identity_card_sha256",
        "idempotency_key",
        "observer",
        "observer_version",
        "observation_mode",
        "policy_version",
        "authorized_usd",
        "input_manifest_sha256",
        "input_manifest",
        "created_at",
    )
)

_LEGAL_TRANSITION = (
    "(OLD.dispatch_state = 'reserved' AND NEW.dispatch_state = 'reserved' "
    "AND OLD.outbound_attempts = 0 AND NEW.outbound_attempts = 0 "
    "AND NEW.failure_code IS NOT NULL AND NEW.observation_payload IS NULL "
    "AND NEW.decision IS NULL AND NEW.reason_codes IS NULL AND NEW.result_sha256 IS NULL) OR "
    "(OLD.dispatch_state = 'reserved' AND NEW.dispatch_state = 'dispatch_uncertain' "
    "AND OLD.outbound_attempts = 0 AND NEW.outbound_attempts = 1 "
    "AND NEW.failure_code IS NULL AND NEW.observation_payload IS NULL "
    "AND NEW.decision IS NULL AND NEW.reason_codes IS NULL AND NEW.result_sha256 IS NULL) OR "
    "(OLD.dispatch_state = 'dispatch_uncertain' AND NEW.dispatch_state = 'dispatch_uncertain' "
    "AND OLD.outbound_attempts = 1 AND NEW.outbound_attempts = 1 "
    "AND NEW.failure_code IS NOT NULL AND NEW.observation_payload IS NULL "
    "AND NEW.decision IS NULL AND NEW.reason_codes IS NULL AND NEW.result_sha256 IS NULL) OR "
    "(OLD.dispatch_state = 'dispatch_uncertain' AND NEW.dispatch_state = 'provider_responded' "
    "AND OLD.outbound_attempts = 1 AND NEW.outbound_attempts = 1 "
    "AND NEW.failure_code IS NULL AND NEW.observation_payload IS NOT NULL "
    "AND NEW.decision IN ('pass', 'needs_review', 'block') "
    "AND NEW.reason_codes IS NOT NULL AND length(NEW.result_sha256) = 64)"
)


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_product_identity_observation_immutable")
    op.execute(
        "CREATE TRIGGER trg_product_identity_observation_immutable "
        "BEFORE UPDATE ON product_identity_observation_runs "
        f"WHEN NOT (({_STATIC_SCOPE_UNCHANGED}) AND ({_LEGAL_TRANSITION})) "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_observation_immutable'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_product_identity_observation_immutable")
    op.execute(
        "CREATE TRIGGER trg_product_identity_observation_immutable "
        "BEFORE UPDATE ON product_identity_observation_runs "
        "BEGIN SELECT RAISE(ABORT, 'product_identity_observation_immutable'); END"
    )
