from __future__ import annotations

import pytest

from photo_organizer.cli.enqueue_product_image_sets import _parser, main
from photo_organizer.handoff.delivery_queue import DeliveryQueueRun


@pytest.mark.asyncio
async def test_enqueue_cli_runs_one_bounded_network_free_pass() -> None:
    calls: list[int] = []

    class Queue:
        async def enqueue_pending(self, *, limit: int) -> DeliveryQueueRun:
            calls.append(limit)
            return DeliveryQueueRun(
                created=1,
                replayed=0,
                blocked={"prepared_image_set_mismatch": 1},
            )

    result = await main(Queue(), limit=25)  # type: ignore[arg-type]

    assert calls == [25]
    assert result.created == 1
    assert result.blocked == {"prepared_image_set_mismatch": 1}


def test_enqueue_cli_help_does_not_require_database_settings() -> None:
    with pytest.raises(SystemExit, match="0"):
        _parser().parse_args(["--help"])
