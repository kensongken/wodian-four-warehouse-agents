import asyncio

import pytest

from photo_organizer.cli.outbox import run


class FakePublisher:
    def __init__(self, counts: list[int], stop: asyncio.Event | None = None) -> None:
        self._counts = counts
        self._stop = stop
        self.calls = 0

    async def publish_pending(self, batch_size: int = 100) -> int:
        assert batch_size == 100
        result = self._counts[self.calls]
        self.calls += 1
        if self._stop is not None:
            self._stop.set()
        return result


@pytest.mark.asyncio
async def test_runner_waits_only_after_an_empty_batch() -> None:
    stop = asyncio.Event()
    publisher = FakePublisher([1, 0])
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        stop.set()

    await run(publisher, stop=stop, sleep=sleep)

    assert publisher.calls == 2
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_runner_finishes_current_batch_after_stop() -> None:
    stop = asyncio.Event()
    publisher = FakePublisher([1], stop)

    async def unexpected_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected sleep {delay}")

    await run(publisher, stop=stop, sleep=unexpected_sleep)

    assert publisher.calls == 1
