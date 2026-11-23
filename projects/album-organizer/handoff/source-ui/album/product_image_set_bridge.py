"""Continuously queue and publish ProductImageSetReadyV1 deliveries."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass

from photo_organizer.config import Settings
from photo_organizer.handoff.delivery_publisher import ProductImageSetDeliveryPublisher
from photo_organizer.handoff.delivery_queue import ProductImageSetDeliveryQueue
from photo_organizer.handoff.http_publisher import ProductImageSetHttpPublisher


@dataclass(frozen=True)
class BridgeRun:
    cycles: int
    queued: int
    replayed: int
    delivered: int
    retryable_failure: int
    permanent_failure: int
    blocked: dict[str, int]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Continuously queue and publish ready product image sets."
    )
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    return parser


async def run(
    queue: ProductImageSetDeliveryQueue,
    publisher: ProductImageSetDeliveryPublisher,
    *,
    stop: asyncio.Event,
    limit: int = 100,
    interval_seconds: float = 5.0,
    max_cycles: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BridgeRun:
    if not 1 <= limit <= 1000:
        raise ValueError("bridge_limit_out_of_range")
    if not 0.25 <= interval_seconds <= 3600:
        raise ValueError("bridge_interval_out_of_range")
    if max_cycles is not None and max_cycles < 1:
        raise ValueError("bridge_max_cycles_must_be_positive")

    cycles = queued = replayed = delivered = retryable = permanent = 0
    blocked: Counter[str] = Counter()
    while not stop.is_set():
        queue_result = await queue.enqueue_pending(limit=limit)
        publish_result = await publisher.publish_pending(limit=limit)
        cycles += 1
        queued += queue_result.created
        replayed += queue_result.replayed
        blocked.update(queue_result.blocked)
        delivered += publish_result.delivered
        retryable += publish_result.retryable_failure
        permanent += publish_result.permanent_failure
        if stop.is_set() or (max_cycles is not None and cycles >= max_cycles):
            break
        await sleep(interval_seconds)
    return BridgeRun(
        cycles=cycles,
        queued=queued,
        replayed=replayed,
        delivered=delivered,
        retryable_failure=retryable,
        permanent_failure=permanent,
        blocked=dict(sorted(blocked.items())),
    )


async def main(
    *,
    endpoint: str,
    limit: int,
    interval_seconds: float,
    once: bool,
    environ: Mapping[str, str] | None = None,
) -> BridgeRun:
    environment = os.environ if environ is None else environ
    secret = environment.get("PRODUCT_IMAGE_SET_SIGNING_SECRET", "").encode()
    if len(secret) < 32:
        raise ValueError("product_image_set_signing_secret_required")
    Settings()  # type: ignore[call-arg]  # Validate runtime settings before DB import.
    from photo_organizer.db import async_session_factory

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    return await run(
        ProductImageSetDeliveryQueue(async_session_factory),
        ProductImageSetDeliveryPublisher(
            async_session_factory,
            ProductImageSetHttpPublisher(endpoint, secret),
        ),
        stop=stop,
        limit=limit,
        interval_seconds=interval_seconds,
        max_cycles=1 if once else None,
    )


def entrypoint() -> None:
    args = _parser().parse_args()
    result = asyncio.run(
        main(
            endpoint=args.endpoint,
            limit=args.limit,
            interval_seconds=args.interval_seconds,
            once=args.once,
        )
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    entrypoint()

