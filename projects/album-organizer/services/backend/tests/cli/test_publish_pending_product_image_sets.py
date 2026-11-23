from __future__ import annotations

import pytest

from photo_organizer.cli.publish_pending_product_image_sets import _parser, main
from photo_organizer.handoff.delivery_publisher import DeliveryPublisherRun


@pytest.mark.asyncio
async def test_pending_publisher_cli_runs_one_bounded_pass() -> None:
    class Worker:
        async def publish_pending(self, *, limit: int) -> DeliveryPublisherRun:
            assert limit == 25
            return DeliveryPublisherRun(1, 1, 0)

    result = await main(Worker(), limit=25)  # type: ignore[arg-type]
    assert result == DeliveryPublisherRun(1, 1, 0)


def test_pending_publisher_help_needs_no_runtime_settings() -> None:
    with pytest.raises(SystemExit, match="0"):
        _parser().parse_args(["--help"])

