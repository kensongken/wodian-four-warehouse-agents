"""Publish one transactional batch from the outbox to Sales Journey."""

import argparse
import asyncio
import json
import time
from collections.abc import Callable

import httpx

from product_engine.config import Settings
from product_engine.db import Database
from product_engine.repositories.events import EventRepository
from product_engine.services.outbox_relay import (
    HttpPoster,
    HttpResponse,
    OutboxRelay,
    RelayResult,
)


class HttpxPoster:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def post(
        self,
        url: str,
        content: bytes,
        headers: dict[str, str],
    ) -> HttpResponse:
        try:
            return await self.client.post(url, content=content, headers=headers)
        except httpx.RequestError as error:
            raise OSError("sales_webhook_transport_error") from error


async def publish_once(
    database: Database,
    http: HttpPoster,
    sales_webhook_url: str,
    signing_secret: bytes,
    *,
    now: Callable[[], int],
    limit: int = 100,
) -> RelayResult:
    async with database.session() as session:
        relay = OutboxRelay(
            EventRepository(session),
            http,
            sales_webhook_url,
            signing_secret,
            now=now,
        )
        return await relay.publish_batch(
            limit=limit,
            topic="sales.strategy.input.updated.v1",
        )


def render_result(result: RelayResult) -> str:
    return json.dumps(
        {
            "delivered": result.delivered,
            "failed": result.failed,
            "retried": result.retried,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


async def run(limit: int) -> RelayResult:
    settings = Settings()  # type: ignore[call-arg]
    database = Database(settings.database_url)
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            return await publish_once(
                database,
                HttpxPoster(client),
                str(settings.sales_webhook_url),
                settings.sales_signing_secret.get_secret_value().encode(),
                now=lambda: int(time.time()),
                limit=limit,
            )
    finally:
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 501))
    args = parser.parse_args()
    result = asyncio.run(run(args.limit))
    print(render_result(result))
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
