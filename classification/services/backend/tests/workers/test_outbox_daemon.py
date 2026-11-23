from __future__ import annotations

import pytest

from product_engine.services.outbox_relay import RelayResult


@pytest.mark.asyncio
async def test_daemon_continues_after_failed_cycle_and_stops_cleanly() -> None:
    from product_engine.workers.outbox_daemon import poll_until_stopped

    attempts = 0
    emitted: list[str] = []
    waits = iter((False, True))

    async def publish() -> RelayResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic database interruption")
        return RelayResult(delivered=2)

    async def wait(_: float) -> bool:
        return next(waits)

    await poll_until_stopped(publish, wait, emitted.append, interval_seconds=0.01)

    assert attempts == 2
    assert emitted == [
        '{"error_code":"outbox_cycle_failed","status":"cycle_failed"}',
        '{"delivered":2,"failed":0,"retried":0,"status":"cycle_completed"}',
    ]


@pytest.mark.asyncio
async def test_daemon_does_not_wait_again_when_stop_is_already_requested() -> None:
    from product_engine.workers.outbox_daemon import poll_until_stopped

    published = False
    waits = 0

    async def publish() -> RelayResult:
        nonlocal published
        published = True
        return RelayResult()

    async def wait(_: float) -> bool:
        nonlocal waits
        waits += 1
        return True

    await poll_until_stopped(publish, wait, lambda _: None, interval_seconds=1)

    assert published is True
    assert waits == 1


def test_worker_parser_rejects_unsafe_poll_interval() -> None:
    from product_engine.workers.outbox_daemon import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--interval-seconds", "0"])
