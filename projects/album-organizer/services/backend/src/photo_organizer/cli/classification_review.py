"""Persist classification candidate events as review items."""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from redis import asyncio as redis_asyncio

from photo_organizer.categorization.review_stream import (
    ClassificationReviewRedisWorker,
    ClassificationReviewStreamHandler,
)
from photo_organizer.config import Settings
from photo_organizer.db import async_session_factory


class ReviewWorker(Protocol):
    async def run_once(self, *, block_ms: int, count: int) -> int: ...


async def run(
    worker: ReviewWorker,
    *,
    stop: asyncio.Event,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    while not stop.is_set():
        processed = await worker.run_once(block_ms=1_000, count=20)
        if stop.is_set():
            return
        if processed == 0:
            await sleep(1.0)


async def main(worker: ReviewWorker | None = None) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    if worker is not None:
        await run(worker, stop=stop)
        return

    settings = Settings()  # type: ignore[call-arg]  # Environment-backed settings.
    redis = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        handler = ClassificationReviewStreamHandler(async_session_factory)
        await run(
            ClassificationReviewRedisWorker(redis, handler),  # type: ignore[arg-type]
            stop=stop,
        )
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
