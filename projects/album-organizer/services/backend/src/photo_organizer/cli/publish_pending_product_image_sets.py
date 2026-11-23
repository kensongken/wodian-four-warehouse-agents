"""Publish one bounded batch from the durable ProductImageSetReady ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from photo_organizer.config import async_database_url
from photo_organizer.handoff.delivery_publisher import (
    DeliveryPublisherRun,
    ProductImageSetDeliveryPublisher,
)
from photo_organizer.handoff.http_publisher import ProductImageSetHttpPublisher


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish queued product image sets to the local Product Engine."
    )
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--limit", type=int, default=100)
    return parser


async def main(
    worker: ProductImageSetDeliveryPublisher | None = None,
    *,
    limit: int = 100,
    endpoint: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> DeliveryPublisherRun:
    if worker is not None:
        return await worker.publish_pending(limit=limit)
    environment = os.environ if environ is None else environ
    database_url = environment.get("DATABASE_URL", "").strip()
    signing_secret = environment.get("PRODUCT_IMAGE_SET_SIGNING_SECRET", "").encode()
    if not database_url:
        raise ValueError("database_url_required")
    if len(signing_secret) < 32:
        raise ValueError("product_image_set_signing_secret_required")
    if endpoint is None:
        raise ValueError("product_image_set_endpoint_required")
    engine = create_async_engine(async_database_url(database_url), pool_pre_ping=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )
    try:
        publisher = ProductImageSetHttpPublisher(endpoint, signing_secret)
        return await ProductImageSetDeliveryPublisher(
            factory, publisher
        ).publish_pending(limit=limit)
    finally:
        await engine.dispose()


def entrypoint() -> None:
    args = _parser().parse_args()
    result = asyncio.run(main(limit=args.limit, endpoint=args.endpoint))
    print(json.dumps(result.__dict__, sort_keys=True))


if __name__ == "__main__":
    entrypoint()

