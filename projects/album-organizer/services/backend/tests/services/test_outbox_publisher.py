import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.services.outbox_publisher import OutboxPublisher


class FakeRedisStream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.error: Exception | None = None

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        if self.error is not None:
            raise self.error
        self.calls.append((stream, fields))
        return f"1710000000-{len(self.calls) - 1}"


@pytest.fixture
def redis_stream() -> FakeRedisStream:
    return FakeRedisStream()


@pytest.fixture
def published_at() -> datetime:
    return datetime(2026, 8, 16, 8, 30, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def clear_outbox(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with auth_session_factory() as session:
        await session.execute(delete(OutboxEvent))
        await session.commit()


@pytest_asyncio.fixture
async def pending_event(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> OutboxEvent:
    event = OutboxEvent(
        topic="ingestion.completed",
        aggregate_id=uuid4(),
        sequence=1,
        payload={"task_id": str(uuid4()), "asset_count": 1000},
        created_at=datetime(2026, 8, 16, 8, tzinfo=UTC),
    )
    async with auth_session_factory() as session:
        session.add(event)
        await session.commit()
        await session.refresh(event)
    return event


@pytest.mark.asyncio
async def test_publisher_marks_only_acknowledged_events(
    auth_session_factory: async_sessionmaker[AsyncSession],
    redis_stream: FakeRedisStream,
    pending_event: OutboxEvent,
    published_at: datetime,
) -> None:
    publisher = OutboxPublisher(
        auth_session_factory,
        redis_stream,
        clock=lambda: published_at,
    )

    assert await publisher.publish_pending() == 1

    async with auth_session_factory() as session:
        persisted = await session.get(OutboxEvent, pending_event.id)
    assert persisted is not None
    assert persisted.published_at == published_at
    assert len(redis_stream.calls) == 1
    stream, fields = redis_stream.calls[0]
    assert stream == "photo-organizer-events"
    assert fields["event_id"] == str(pending_event.id)
    assert fields["topic"] == "ingestion.completed"
    assert fields["aggregate_id"] == str(pending_event.aggregate_id)
    assert fields["sequence"] == "1"
    assert fields["occurred_at"] == pending_event.created_at.isoformat()
    assert json.loads(fields["payload"]) == pending_event.payload


@pytest.mark.asyncio
async def test_publisher_retries_unacknowledged_event(
    auth_session_factory: async_sessionmaker[AsyncSession],
    redis_stream: FakeRedisStream,
    pending_event: OutboxEvent,
    published_at: datetime,
) -> None:
    redis_stream.error = ConnectionError("down")
    publisher = OutboxPublisher(
        auth_session_factory,
        redis_stream,
        clock=lambda: published_at,
    )

    with pytest.raises(ConnectionError, match="down"):
        await publisher.publish_pending()

    async with auth_session_factory() as session:
        persisted = await session.get(OutboxEvent, pending_event.id)
    assert persisted is not None
    assert persisted.published_at is None


@pytest.mark.asyncio
async def test_publisher_respects_batch_order_and_limit(
    auth_session_factory: async_sessionmaker[AsyncSession],
    redis_stream: FakeRedisStream,
    pending_event: OutboxEvent,
    published_at: datetime,
) -> None:
    second = OutboxEvent(
        topic="ingestion.completed",
        aggregate_id=uuid4(),
        sequence=1,
        payload={"asset_count": 2},
        created_at=datetime(2026, 8, 16, 8, 1, tzinfo=UTC),
    )
    async with auth_session_factory() as session:
        session.add(second)
        await session.commit()
        await session.refresh(second)
    publisher = OutboxPublisher(
        auth_session_factory,
        redis_stream,
        clock=lambda: published_at,
    )

    assert await publisher.publish_pending(batch_size=1) == 1

    async with auth_session_factory() as session:
        remaining = list(
            (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                )
            ).all()
        )
    assert len(remaining) == 1
    assert redis_stream.calls[0][1]["event_id"] == str(pending_event.id)
