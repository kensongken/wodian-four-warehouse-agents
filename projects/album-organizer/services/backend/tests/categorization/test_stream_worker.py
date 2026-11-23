from __future__ import annotations

from collections.abc import Mapping

import pytest

from photo_organizer.categorization.review_stream import (
    ClassificationReviewRedisWorker,
)
from photo_organizer.categorization.stream import (
    CategorizationRedisWorker,
    DeliveryOutcome,
)


class FakeHandler:
    def __init__(self, outcome: DeliveryOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str | bytes, Mapping[object, object]]] = []

    async def handle(
        self, stream_id: str | bytes, fields: Mapping[object, object]
    ) -> DeliveryOutcome:
        self.calls.append((stream_id, fields))
        return self.outcome


class FailingHandler:
    async def handle(
        self, stream_id: str | bytes, fields: Mapping[object, object]
    ) -> DeliveryOutcome:
        del stream_id, fields
        raise ConnectionError("database unavailable")


class FakeConsumerRedis:
    def __init__(self) -> None:
        self.group_calls: list[tuple[str, str, str, bool]] = []
        self.claimed: list[tuple[bytes, dict[bytes, bytes]]] = []
        self.fresh: list[tuple[bytes, dict[bytes, bytes]]] = []
        self.read_calls = 0
        self.acks: list[tuple[str, str, bytes | str]] = []

    async def xgroup_create(
        self, name: str, groupname: str, id: str, mkstream: bool
    ) -> bool:
        self.group_calls.append((name, groupname, id, mkstream))
        return True

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        count: int,
    ) -> list[object]:
        del name, groupname, consumername, min_idle_time, start_id, count
        entries = self.claimed
        self.claimed = []
        return [b"0-0", entries, []]

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int,
        block: int,
    ) -> list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]:
        del groupname, consumername, streams, count, block
        self.read_calls += 1
        entries = self.fresh
        self.fresh = []
        return [(b"photo-organizer-events", entries)] if entries else []

    async def xack(self, name: str, groupname: str, stream_id: bytes | str) -> int:
        self.acks.append((name, groupname, stream_id))
        return 1


def _entry(suffix: int) -> tuple[bytes, dict[bytes, bytes]]:
    return (
        f"1710000000-{suffix}".encode(),
        {b"topic": b"album.product.snapshot.confirmed.v1"},
    )


@pytest.mark.asyncio
async def test_worker_creates_group_and_acknowledges_durable_outcome() -> None:
    redis = FakeConsumerRedis()
    redis.fresh = [_entry(1)]
    handler = FakeHandler(DeliveryOutcome(acknowledge=True, status="completed"))
    worker = CategorizationRedisWorker(
        redis,
        handler,
        consumer_name="worker-test",
    )

    processed = await worker.run_once(block_ms=1)

    assert processed == 1
    assert redis.group_calls == [
        ("photo-organizer-events", "stage2-categorization-v1", "0", True)
    ]
    assert handler.calls == [_entry(1)]
    assert redis.acks == [
        ("photo-organizer-events", "stage2-categorization-v1", b"1710000000-1")
    ]


@pytest.mark.asyncio
async def test_worker_recovers_stale_pending_before_reading_fresh_entries() -> None:
    redis = FakeConsumerRedis()
    redis.claimed = [_entry(2)]
    redis.fresh = [_entry(3)]
    handler = FakeHandler(DeliveryOutcome(acknowledge=True, status="completed"))
    worker = CategorizationRedisWorker(redis, handler, consumer_name="worker-test")

    processed = await worker.run_once(block_ms=1)

    assert processed == 1
    assert redis.read_calls == 0
    assert handler.calls == [_entry(2)]
    assert redis.fresh == [_entry(3)]


@pytest.mark.asyncio
async def test_worker_does_not_acknowledge_retryable_delivery() -> None:
    redis = FakeConsumerRedis()
    redis.fresh = [_entry(4)]
    handler = FakeHandler(DeliveryOutcome(acknowledge=False, status="retryable_failed"))
    worker = CategorizationRedisWorker(redis, handler, consumer_name="worker-test")

    processed = await worker.run_once(block_ms=1)

    assert processed == 1
    assert redis.acks == []


@pytest.mark.asyncio
async def test_review_worker_uses_an_independent_consumer_group() -> None:
    redis = FakeConsumerRedis()
    redis.fresh = [_entry(5)]
    handler = FakeHandler(DeliveryOutcome(acknowledge=True, status="completed"))
    worker = ClassificationReviewRedisWorker(
        redis,
        handler,
        consumer_name="review-worker-test",
    )

    assert await worker.run_once(block_ms=1) == 1

    assert redis.group_calls == [
        ("photo-organizer-events", "stage2-classification-review-v1", "0", True)
    ]
    assert redis.acks == [
        (
            "photo-organizer-events",
            "stage2-classification-review-v1",
            b"1710000000-5",
        )
    ]


@pytest.mark.asyncio
async def test_worker_never_acknowledges_when_handler_raises() -> None:
    redis = FakeConsumerRedis()
    redis.fresh = [_entry(6)]
    worker = ClassificationReviewRedisWorker(
        redis,
        FailingHandler(),
        consumer_name="review-worker-test",
    )

    with pytest.raises(ConnectionError, match="database unavailable"):
        await worker.run_once(block_ms=1)

    assert redis.acks == []
