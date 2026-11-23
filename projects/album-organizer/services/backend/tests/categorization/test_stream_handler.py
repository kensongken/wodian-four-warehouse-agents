from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.snapshots import AlbumProductSnapshotService
from photo_organizer.categorization.consumer import CategorizationUnavailable
from photo_organizer.categorization.contracts import (
    CategorizationRequest,
    ConfirmedSnapshotBatch,
    EngineCandidate,
    ReasonCode,
)
from photo_organizer.categorization.stream import (
    FAILURE_TOPIC,
    CategorizationDeliveryService,
    CategorizationStreamHandler,
    DeliveryOutcome,
)
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.categorization_delivery import CategorizationDelivery
from photo_organizer.models.outbox import OutboxEvent

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "contracts/fixtures/first-10-sku-categorization-events.json"
)
NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


class FixtureEngine:
    engine_name = "fixture-categorizer"
    engine_version = "1.0"

    def __init__(self) -> None:
        self.available = True
        self.calls = 0

    async def classify(
        self, request: CategorizationRequest
    ) -> tuple[EngineCandidate, ...]:
        self.calls += 1
        if not self.available:
            raise CategorizationUnavailable
        return (
            EngineCandidate(
                level_1=request.seed.level_1_candidate,
                level_2=request.seed.level_2_candidate,
                confidence=0.91,
                reason_codes=(ReasonCode.SEED_MATCH,),
            ),
        )


class InspectingEngine(FixtureEngine):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self.snapshot_count_seen = -1

    async def classify(
        self, request: CategorizationRequest
    ) -> tuple[EngineCandidate, ...]:
        async with self._session_factory() as session:
            count = await session.scalar(select(func.count(AlbumProductSnapshot.id)))
        self.snapshot_count_seen = int(count or 0)
        return await super().classify(request)


def _fields(*, event_id: UUID | None = None) -> dict[str, str]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return {
        "event_id": str(event_id or uuid4()),
        "topic": "album.product.snapshot.confirmed.v1",
        "aggregate_id": payload["batch_id"],
        "sequence": "1",
        "occurred_at": NOW.isoformat(),
        "payload": json.dumps(payload, ensure_ascii=False),
    }


@pytest_asyncio.fixture(autouse=True)
async def clear_deliveries(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with auth_session_factory() as session:
        await session.execute(delete(AlbumProductSnapshot))
        await session.execute(delete(CategorizationDelivery))
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.topic.in_(
                    (
                        "album.product.classification.candidates.v1",
                        "album.product.classification.failed.v1",
                    )
                )
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_snapshots_commit_before_the_engine_is_called(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = InspectingEngine(auth_session_factory)
    handler = CategorizationStreamHandler(
        auth_session_factory,
        engine,
        clock=lambda: NOW,
    )

    outcome = await handler.handle("1710000000-snapshot", _fields())

    assert outcome == DeliveryOutcome(acknowledge=True, status="completed")
    assert engine.snapshot_count_seen == 10


@pytest.mark.asyncio
async def test_snapshot_conflict_is_terminal_and_never_calls_engine(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fields = _fields()
    source_payload = json.loads(fields["payload"])
    changed_payload = json.loads(fields["payload"])
    changed_payload["events"][0]["images"][0]["sha256"] = "f" * 64
    await AlbumProductSnapshotService(auth_session_factory).ingest(
        ConfirmedSnapshotBatch.model_validate(changed_payload)
    )
    engine = FixtureEngine()
    handler = CategorizationStreamHandler(
        auth_session_factory,
        engine,
        clock=lambda: NOW,
    )

    outcome = await handler.handle("1710000001-snapshot", fields)

    assert outcome == DeliveryOutcome(acknowledge=True, status="terminal_failed")
    assert engine.calls == 0
    async with auth_session_factory() as session:
        failure = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.failed.v1"
            )
        )
    assert failure is not None
    assert failure.payload["code"] == "snapshot_identity_conflict"
    assert source_payload["events"][0]["images"][0]["sha256"] != "f" * 64


@pytest.mark.asyncio
async def test_first_ten_skus_commit_once_before_acknowledgement(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FixtureEngine()
    handler = CategorizationStreamHandler(
        auth_session_factory,
        engine,
        clock=lambda: NOW,
    )
    fields = _fields()

    first = await handler.handle("1710000000-0", fields)
    replay = await handler.handle("1710000000-0", fields)

    assert first.acknowledge is True
    assert first.status == "completed"
    assert replay.acknowledge is True
    assert replay.status == "completed"
    assert engine.calls == 10
    async with auth_session_factory() as session:
        delivery = await session.scalar(select(CategorizationDelivery))
        outbox = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.candidates.v1"
            )
        )
        assert await session.scalar(select(func.count(CategorizationDelivery.id))) == 1
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic == "album.product.classification.candidates.v1"
                )
            )
            == 1
        )
    assert delivery is not None
    assert delivery.status == "completed"
    assert delivery.attempt_count == 1
    assert delivery.completed_at == NOW
    assert outbox is not None
    assert len(outbox.payload["events"]) == 10
    assert outbox.payload["failures"] == []


@pytest.mark.asyncio
async def test_retryable_batch_stays_pending_then_recovers(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = MutableClock()
    engine = FixtureEngine()
    engine.available = False
    handler = CategorizationStreamHandler(
        auth_session_factory,
        engine,
        clock=clock,
    )
    fields = _fields()

    first = await handler.handle("1710000001-0", fields)
    too_early = await handler.handle("1710000001-0", fields)
    engine.available = True
    clock.advance(seconds=2)
    recovered = await handler.handle("1710000001-0", fields)

    assert first.acknowledge is False
    assert first.status == "retryable_failed"
    assert too_early.acknowledge is False
    assert too_early.status == "retry_later"
    assert recovered.acknowledge is True
    assert recovered.status == "completed"
    assert engine.calls == 20
    async with auth_session_factory() as session:
        delivery = await session.scalar(select(CategorizationDelivery))
    assert delivery is not None
    assert delivery.attempt_count == 2


@pytest.mark.asyncio
async def test_expired_owner_cannot_publish_over_new_generation(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = MutableClock()
    service = CategorizationDeliveryService(
        auth_session_factory,
        clock=clock,
        lease_duration=timedelta(seconds=30),
    )
    fields = _fields()
    source_event_id = UUID(fields["event_id"])
    payload = json.loads(fields["payload"])

    old = await service.claim(
        source_event_id=source_event_id,
        source_stream_id="1710000002-0",
        aggregate_id=UUID(fields["aggregate_id"]),
        topic=fields["topic"],
        payload=payload,
    )
    assert old.claim is not None
    clock.advance(seconds=31)
    new = await service.claim(
        source_event_id=source_event_id,
        source_stream_id="1710000002-0",
        aggregate_id=UUID(fields["aggregate_id"]),
        topic=fields["topic"],
        payload=payload,
    )
    assert new.claim is not None
    assert new.claim.generation == old.claim.generation + 1

    stale_result = await service.complete(
        old.claim,
        result_payload={"events": [], "failures": []},
        aggregate_id=UUID(fields["aggregate_id"]),
    )
    current_result = await service.complete(
        new.claim,
        result_payload={"events": [], "failures": []},
        aggregate_id=UUID(fields["aggregate_id"]),
    )

    assert stale_result is False
    assert current_result is True
    async with auth_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic == "album.product.classification.candidates.v1"
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_invalid_target_payload_is_terminal_and_safe(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FixtureEngine()
    handler = CategorizationStreamHandler(
        auth_session_factory,
        engine,
        clock=lambda: NOW,
    )
    fields = _fields()
    fields["payload"] = json.dumps({"customer_phone": "+886900000000"})

    outcome = await handler.handle("1710000003-0", fields)

    assert outcome.acknowledge is True
    assert outcome.status == "terminal_failed"
    assert engine.calls == 0
    async with auth_session_factory() as session:
        delivery = await session.scalar(select(CategorizationDelivery))
        outbox = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.failed.v1"
            )
        )
    assert delivery is not None
    assert delivery.last_error_code == "invalid_message"
    assert delivery.input_payload == {}
    assert outbox is not None
    serialized = json.dumps(outbox.payload)
    assert "customer_phone" not in serialized
    assert "+886" not in serialized


@pytest.mark.asyncio
async def test_unrelated_topic_is_acknowledged_without_persistence(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = FixtureEngine()
    handler = CategorizationStreamHandler(auth_session_factory, engine)
    fields = _fields()
    fields["topic"] = "ingestion.completed"

    outcome = await handler.handle("1710000004-0", fields)

    assert outcome.acknowledge is True
    assert outcome.status == "ignored"
    assert engine.calls == 0
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(CategorizationDelivery.id))) == 0


@pytest.mark.asyncio
async def test_eighth_unavailable_attempt_is_durably_exhausted(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = MutableClock()
    engine = FixtureEngine()
    engine.available = False
    handler = CategorizationStreamHandler(
        auth_session_factory,
        engine,
        clock=clock,
    )
    fields = _fields()

    outcomes = []
    for delay in (0, 2, 10, 30, 120, 300, 900, 1800):
        clock.advance(seconds=delay)
        outcomes.append(await handler.handle("1710000005-0", fields))

    assert [outcome.status for outcome in outcomes[:-1]] == ["retryable_failed"] * 7
    assert outcomes[-1].status == "terminal_failed"
    assert outcomes[-1].acknowledge is True
    assert engine.calls == 80
    async with auth_session_factory() as session:
        delivery = await session.scalar(select(CategorizationDelivery))
        failure = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.topic == FAILURE_TOPIC)
        )
    assert delivery is not None
    assert delivery.attempt_count == 8
    assert delivery.last_error_code == "categorization_retry_exhausted"
    assert failure is not None
    assert failure.payload["code"] == "categorization_retry_exhausted"


@pytest.mark.asyncio
async def test_malformed_target_envelope_gets_a_durable_safe_identity(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handler = CategorizationStreamHandler(
        auth_session_factory,
        FixtureEngine(),
        clock=lambda: NOW,
    )
    fields = {
        "topic": "album.product.snapshot.confirmed.v1",
        "event_id": "not-a-uuid",
        "payload": "not-json",
    }

    outcome = await handler.handle("1710000006-0", fields)

    assert outcome == DeliveryOutcome(
        acknowledge=True,
        status="terminal_failed",
    )
    async with auth_session_factory() as session:
        delivery = await session.scalar(select(CategorizationDelivery))
        failure = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.topic == FAILURE_TOPIC)
        )
    assert delivery is not None
    assert delivery.source_stream_id == "1710000006-0"
    assert delivery.input_payload == {}
    assert delivery.last_error_code == "invalid_message"
    assert failure is not None
    serialized = json.dumps(failure.payload)
    assert "not-json" not in serialized
    assert "not-a-uuid" not in serialized
