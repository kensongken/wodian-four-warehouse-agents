"""Normalize pending observation JSON nulls to SQL NULL."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0041_observation_json_nulls"
down_revision: str | None = "0040_local_identity_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        trigger = connection.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_product_identity_observation_immutable'"
            )
        ).scalar_one()
        op.execute("DROP TRIGGER trg_product_identity_observation_immutable")
        op.execute(
            "UPDATE product_identity_observation_runs SET observation_payload=NULL "
            "WHERE observation_payload='null'"
        )
        op.execute(
            "UPDATE product_identity_observation_runs SET reason_codes=NULL "
            "WHERE reason_codes='null'"
        )
        op.execute(trigger)
        return
    op.execute(
        "UPDATE product_identity_observation_runs SET observation_payload=NULL "
        "WHERE CAST(observation_payload AS TEXT)='null'"
    )
    op.execute(
        "UPDATE product_identity_observation_runs SET reason_codes=NULL "
        "WHERE CAST(reason_codes AS TEXT)='null'"
    )


def downgrade() -> None:
    # SQL NULL is the canonical representation; no semantic rollback is required.
    pass
