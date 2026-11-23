"""Publish durable ingestion events to the Phase 2 Redis stream."""

import asyncio
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress

from redis import asyncio as redis_asyncio

from photo_organizer.config import Settings
from photo_organizer.db import async_session_factory
from photo_organizer.services.outbox_publisher import OutboxPublisher


async def run(
    publisher: OutboxPublisher,
    *,
    stop: asyncio.Event,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Drain batches until stopped, idling only when no row was available."""
    while not stop.is_set():
        published = await publisher.publish_pending(batch_size=100)
        if stop.is_set():
            return
        if published == 0:
            await sleep(1.0)


async def main(publisher: OutboxPublisher | None = None) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    if publisher is not None:
        await run(publisher, stop=stop)
        return

    settings = Settings()  # type: ignore[call-arg]  # Environment-backed settings.
    redis = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        await run(
            OutboxPublisher(async_session_factory, redis),  # type: ignore[arg-type]
            stop=stop,
        )
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
