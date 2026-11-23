"""Create durable ProductImageSetReadyV1 delivery records in one bounded pass.

This command never publishes HTTP requests or reads image bytes.  It is safe
for a local scheduler to invoke repeatedly; the delivery ledger owns idempotency.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from photo_organizer.config import Settings
from photo_organizer.handoff.delivery_queue import (
    DeliveryQueueRun,
    ProductImageSetDeliveryQueue,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Queue confirmed synthetic-safe product image-set deliveries."
    )
    parser.add_argument("--limit", type=int, default=100)
    return parser


async def main(
    queue: ProductImageSetDeliveryQueue | None = None,
    *,
    limit: int = 100,
) -> DeliveryQueueRun:
    if queue is not None:
        return await queue.enqueue_pending(limit=limit)
    Settings()  # type: ignore[call-arg]  # Environment-backed database settings.
    from photo_organizer.db import async_session_factory

    return await ProductImageSetDeliveryQueue(async_session_factory).enqueue_pending(
        limit=limit
    )


def entrypoint() -> None:
    args = _parser().parse_args()
    result = asyncio.run(main(limit=args.limit))
    print(
        json.dumps(
            {
                "blocked": result.blocked,
                "created": result.created,
                "replayed": result.replayed,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    entrypoint()
