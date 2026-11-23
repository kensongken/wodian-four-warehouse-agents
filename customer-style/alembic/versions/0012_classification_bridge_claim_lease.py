"""Add owner leases to the local classification bridge fence."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_classification_bridge_claim_lease"
down_revision: str | None = "0011_classification_bridge_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("classification_bridge_claims") as batch:
        batch.add_column(sa.Column("owner_token", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("classification_bridge_claims") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("owner_token")
