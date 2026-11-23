"""Scope inbox event idempotency by tenant.

Revision ID: 0002_tenant_scope_inbox_events
Revises: 0001_integration_spine
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_tenant_scope_inbox_events"
down_revision: str | None = "0001_integration_spine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM inbox_events) THEN
                RAISE EXCEPTION 'manual tenant backfill required for inbox_events';
            END IF;
        END
        $$
        """
    )
    op.add_column("inbox_events", sa.Column("tenant_id", UUID, nullable=False))
    op.drop_constraint("uq_inbox_events_source_event", "inbox_events", type_="unique")
    op.create_unique_constraint(
        "uq_inbox_events_tenant_source_event",
        "inbox_events",
        ["tenant_id", "source_system", "event_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_inbox_events_tenant_source_event", "inbox_events", type_="unique")
    op.create_unique_constraint(
        "uq_inbox_events_source_event",
        "inbox_events",
        ["source_system", "event_id"],
    )
    op.drop_column("inbox_events", "tenant_id")
