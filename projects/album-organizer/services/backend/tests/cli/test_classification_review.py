import asyncio

import pytest

from photo_organizer.cli.classification_review import run


class FakeReviewWorker:
    def __init__(self, counts: list[int], stop: asyncio.Event | None = None) -> None:
        self._counts = counts
        self._stop = stop
        self.calls: list[tuple[int, int]] = []

    async def run_once(self, *, block_ms: int, count: int) -> int:
        self.calls.append((block_ms, count))
        result = self._counts[len(self.calls) - 1]
        if self._stop is not None:
            self._stop.set()
        return result


@pytest.mark.asyncio
async def test_review_runner_idles_only_after_an_empty_read() -> None:
    stop = asyncio.Event()
    worker = FakeReviewWorker([1, 0])
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        stop.set()

    await run(worker, stop=stop, sleep=sleep)

    assert worker.calls == [(1_000, 20), (1_000, 20)]
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_review_runner_finishes_current_read_after_stop() -> None:
    stop = asyncio.Event()
    worker = FakeReviewWorker([1], stop)

    async def unexpected_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected sleep {delay}")

    await run(worker, stop=stop, sleep=unexpected_sleep)

    assert worker.calls == [(1_000, 20)]
