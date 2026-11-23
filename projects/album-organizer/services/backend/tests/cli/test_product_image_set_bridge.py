from __future__ import annotations

import asyncio

import pytest

from photo_organizer.cli.product_image_set_bridge import BridgeRun, run
from photo_organizer.handoff.delivery_publisher import DeliveryPublisherRun
from photo_organizer.handoff.delivery_queue import DeliveryQueueRun


@pytest.mark.asyncio
async def test_bridge_runs_queue_before_publisher_and_stops_cleanly() -> None:
    calls: list[str] = []
    stop = asyncio.Event()

    class Queue:
        async def enqueue_pending(self, *, limit: int) -> DeliveryQueueRun:
            calls.append(f"queue:{limit}")
            return DeliveryQueueRun(1, 0, {})

    class Publisher:
        async def publish_pending(self, *, limit: int) -> DeliveryPublisherRun:
            calls.append(f"publish:{limit}")
            stop.set()
            return DeliveryPublisherRun(1, 0, 0)

    result = await run(
        Queue(),  # type: ignore[arg-type]
        Publisher(),  # type: ignore[arg-type]
        stop=stop,
        limit=25,
        interval_seconds=3,
    )

    assert calls == ["queue:25", "publish:25"]
    assert result == BridgeRun(
        cycles=1,
        queued=1,
        replayed=0,
        delivered=1,
        retryable_failure=0,
        permanent_failure=0,
        blocked={},
    )


@pytest.mark.asyncio
async def test_idle_bridge_waits_then_runs_next_cycle() -> None:
    stop = asyncio.Event()
    sleeps: list[float] = []
    cycles = 0

    class Queue:
        async def enqueue_pending(self, *, limit: int) -> DeliveryQueueRun:
            del limit
            return DeliveryQueueRun(0, 0, {})

    class Publisher:
        async def publish_pending(self, *, limit: int) -> DeliveryPublisherRun:
            nonlocal cycles
            del limit
            cycles += 1
            if cycles == 2:
                stop.set()
            return DeliveryPublisherRun(0, 0, 0)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await run(
        Queue(),  # type: ignore[arg-type]
        Publisher(),  # type: ignore[arg-type]
        stop=stop,
        limit=10,
        interval_seconds=7,
        sleep=sleep,
    )

    assert result.cycles == 2
    assert sleeps == [7]


@pytest.mark.asyncio
async def test_bridge_rejects_unsafe_interval() -> None:
    with pytest.raises(ValueError, match="bridge_interval_out_of_range"):
        await run(  # type: ignore[arg-type]
            object(), object(), stop=asyncio.Event(), interval_seconds=0
        )
