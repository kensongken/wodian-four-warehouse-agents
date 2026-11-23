from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from product_engine.domain.errors import EventConflict
from product_engine.models.events import InboxEvent, OutboxEvent


@dataclass(frozen=True)
class ClaimResult:
    disposition: Literal["new", "duplicate"]


def inbox_claim_statement(
    *,
    row_id: UUID,
    tenant_id: UUID,
    source_system: str,
    event_id: UUID,
    payload_sha256: str,
) -> Executable:
    return (
        insert(InboxEvent)
        .values(
            id=row_id,
            tenant_id=tenant_id,
            source_system=source_system,
            event_id=event_id,
            payload_sha256=payload_sha256,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "source_system", "event_id"])
        .returning(InboxEvent.id)
    )


def pending_outbox_statement(limit: int, *, topic: str | None = None) -> Executable:
    conditions = [
        OutboxEvent.status == "pending",
        OutboxEvent.available_at <= func.now(),
    ]
    if topic is not None:
        conditions.append(OutboxEvent.topic == topic)
    return (
        select(OutboxEvent)
        .where(*conditions)
        .order_by(OutboxEvent.available_at, OutboxEvent.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim_inbox(
        self,
        tenant_id: UUID,
        source_system: str,
        event_id: UUID,
        payload_sha256: str,
    ) -> ClaimResult:
        statement = inbox_claim_statement(
            row_id=uuid4(),
            tenant_id=tenant_id,
            source_system=source_system,
            event_id=event_id,
            payload_sha256=payload_sha256,
        )
        inserted_id = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted_id is not None:
            return ClaimResult(disposition="new")

        existing_hash = (
            await self.session.execute(
                select(InboxEvent.payload_sha256).where(
                    InboxEvent.tenant_id == tenant_id,
                    InboxEvent.source_system == source_system,
                    InboxEvent.event_id == event_id,
                )
            )
        ).scalar_one()
        if existing_hash != payload_sha256:
            raise EventConflict("event_id_reused_with_different_payload")
        return ClaimResult(disposition="duplicate")

    async def append_outbox(
        self,
        tenant_id: UUID,
        topic: str,
        aggregate_key: str,
        payload: dict[str, object],
    ) -> UUID:
        event_id = uuid4()
        self.session.add(
            OutboxEvent(
                id=event_id,
                tenant_id=tenant_id,
                topic=topic,
                aggregate_key=aggregate_key,
                payload=payload,
                status="pending",
                attempts=0,
            )
        )
        return event_id

    async def claim_pending(
        self, limit: int, now: int, *, topic: str | None = None
    ) -> list[OutboxEvent]:
        del now
        result = await self.session.execute(pending_outbox_statement(limit, topic=topic))
        return list(result.scalars().all())

    async def mark_delivered(self, event_id: UUID, delivered_at: int) -> None:
        await self.session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, OutboxEvent.status == "pending")
            .values(
                status="delivered",
                delivered_at=datetime.fromtimestamp(delivered_at, UTC),
                last_error_code=None,
            )
        )

    async def record_delivery_failure(
        self,
        event_id: UUID,
        attempts: int,
        available_at: int,
        error_code: str,
        failed: bool,
    ) -> None:
        await self.session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, OutboxEvent.status == "pending")
            .values(
                status="failed" if failed else "pending",
                attempts=attempts,
                available_at=datetime.fromtimestamp(available_at, UTC),
                last_error_code=error_code,
            )
        )
