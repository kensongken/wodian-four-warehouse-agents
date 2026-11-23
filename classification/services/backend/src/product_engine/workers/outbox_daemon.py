"""Continuously deliver Sales Journey notifications from the transactional outbox."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time
from collections.abc import Awaitable, Callable, Sequence

import httpx

from product_engine.config import Settings
from product_engine.db import Database
from product_engine.services.outbox_relay import RelayResult
from product_engine.workers.outbox_publisher import HttpxPoster, publish_once

PublishCycle = Callable[[], Awaitable[RelayResult]]
WaitForNextCycle = Callable[[float], Awaitable[bool]]
Emit = Callable[[str], None]


def render_cycle(result: RelayResult) -> str:
    return json.dumps(
        {
            "delivered": result.delivered,
            "failed": result.failed,
            "retried": result.retried,
            "status": "cycle_completed",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def render_cycle_failure() -> str:
    return json.dumps(
        {"error_code": "outbox_cycle_failed", "status": "cycle_failed"},
        separators=(",", ":"),
        sort_keys=True,
    )


async def poll_until_stopped(
    publish: PublishCycle,
    wait_for_next_cycle: WaitForNextCycle,
    emit: Emit,
    *,
    interval_seconds: float,
) -> None:
    while True:
        try:
            result = await publish()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed cycle must not kill the daemon
            emit(render_cycle_failure())
        else:
            emit(render_cycle(result))
        if await wait_for_next_cycle(interval_seconds):
            return


async def run(limit: int, interval_seconds: float) -> None:
    settings = Settings()  # type: ignore[call-arg]
    database = Database(settings.database_url)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(stop_signal, stop_event.set)

    async def wait_for_next_cycle(delay: float) -> bool:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:

            async def publish() -> RelayResult:
                return await publish_once(
                    database,
                    HttpxPoster(client),
                    str(settings.sales_webhook_url),
                    settings.sales_signing_secret.get_secret_value().encode(),
                    now=lambda: int(time.time()),
                    limit=limit,
                )

            await poll_until_stopped(
                publish,
                wait_for_next_cycle,
                lambda message: print(message, flush=True),
                interval_seconds=interval_seconds,
            )
    finally:
        await database.dispose()


def positive_interval(value: str) -> float:
    interval = float(value)
    if not 0.05 <= interval <= 3600:
        raise argparse.ArgumentTypeError("interval must be between 0.05 and 3600 seconds")
    return interval


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 501))
    parser.add_argument("--interval-seconds", type=positive_interval, default=2.0)
    return parser.parse_args(arguments)


def main() -> int:
    args = parse_args()
    asyncio.run(run(args.limit, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
