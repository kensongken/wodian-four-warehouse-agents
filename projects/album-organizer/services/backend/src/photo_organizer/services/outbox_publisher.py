import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.repositories.unit_of_work import UnitOfWork


class RedisStream(Protocol):
    async def xadd(self, name: str, fields: Mapping[str, str]) -> str | bytes: ...


class OutboxPublisher:
    """Publish committed outbox rows with at-least-once delivery."""

    stream_name = "photo-organizer-events"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: RedisStream,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._clock = clock or (lambda: datetime.now(UTC))

    async def publish_pending(self, batch_size: int = 100) -> int:
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        async with UnitOfWork(self._session_factory) as unit_of_work:
            statement = (
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            events = list((await unit_of_work.session.scalars(statement)).all())
            for event in events:
                await self._redis.xadd(self.stream_name, self._fields(event))
                event.published_at = self._clock()
            await unit_of_work.session.flush()
            return len(events)

    @staticmethod
    def _fields(event: OutboxEvent) -> dict[str, str]:
        return {
            "event_id": str(event.id),
            "topic": event.topic,
            "aggregate_id": str(event.aggregate_id),
            "sequence": str(event.sequence),
            "occurred_at": event.created_at.isoformat(),
            "payload": json.dumps(
                event.payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
