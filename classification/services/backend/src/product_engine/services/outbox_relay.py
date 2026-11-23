import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID

from pydantic import ValidationError

from product_engine.contracts.sales_strategy import SalesStrategyInputUpdatedV1
from product_engine.security.signatures import sign_request

RETRY_DELAYS_SECONDS = (30, 120, 600, 1800, 7200)


class PendingOutboxEvent(Protocol):
    id: UUID
    tenant_id: UUID
    topic: str
    payload: dict[str, object]
    attempts: int


class OutboxStore(Protocol):
    async def claim_pending(
        self, limit: int, now: int, *, topic: str | None = None
    ) -> Sequence[PendingOutboxEvent]: ...

    async def mark_delivered(self, event_id: UUID, delivered_at: int) -> None: ...

    async def record_delivery_failure(
        self,
        event_id: UUID,
        attempts: int,
        available_at: int,
        error_code: str,
        failed: bool,
    ) -> None: ...


class HttpResponse(Protocol):
    status_code: int


class HttpPoster(Protocol):
    async def post(self, url: str, content: bytes, headers: dict[str, str]) -> HttpResponse: ...


@dataclass(frozen=True)
class RelayResult:
    delivered: int = 0
    retried: int = 0
    failed: int = 0


class OutboxRelay:
    def __init__(
        self,
        store: OutboxStore,
        http: HttpPoster,
        sales_webhook_url: str,
        signing_secret: bytes,
        *,
        now: Callable[[], int],
    ) -> None:
        self.store = store
        self.http = http
        self.sales_webhook_url = sales_webhook_url
        self.signing_secret = signing_secret
        self.now = now

    async def publish_batch(self, limit: int = 100, *, topic: str | None = None) -> RelayResult:
        timestamp = self.now()
        events = await self.store.claim_pending(limit, timestamp, topic=topic)
        delivered = 0
        retried = 0
        failed = 0
        for event in events:
            outcome = await self._publish(event, timestamp)
            if outcome == "delivered":
                delivered += 1
            elif outcome == "retried":
                retried += 1
            else:
                failed += 1
        return RelayResult(delivered=delivered, retried=retried, failed=failed)

    async def _publish(self, event: PendingOutboxEvent, timestamp: int) -> str:
        if event.topic == "sales.strategy.input.updated.v1":
            try:
                sales_update = SalesStrategyInputUpdatedV1.model_validate(event.payload)
            except ValidationError:
                return await self._record_failure(
                    event,
                    timestamp,
                    "invalid_sales_strategy_event",
                    retry=False,
                )
            if sales_update.tenant_id != event.tenant_id:
                return await self._record_failure(
                    event,
                    timestamp,
                    "invalid_sales_strategy_event",
                    retry=False,
                )
        body = json.dumps(
            event.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        path = urlparse(self.sales_webhook_url).path or "/"
        headers = {
            "Content-Type": "application/json",
            "X-Source-System": "product-image-engine",
            "X-Tenant-Id": str(event.tenant_id),
            "X-Event-Topic": event.topic,
            "X-Outbox-Event-Id": str(event.id),
            "X-Signature-Timestamp": str(timestamp),
            "X-Signature": sign_request(
                self.signing_secret,
                timestamp,
                "product-image-engine",
                event.tenant_id,
                "POST",
                path,
                body,
            ),
        }
        try:
            response = await self.http.post(self.sales_webhook_url, body, headers)
        except OSError:
            return await self._record_failure(event, timestamp, "connect_timeout")
        if 200 <= response.status_code < 300:
            await self.store.mark_delivered(event.id, timestamp)
            return "delivered"
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            return await self._record_failure(
                event,
                timestamp,
                f"http_{response.status_code}",
            )
        return await self._record_failure(
            event,
            timestamp,
            f"http_{response.status_code}",
            retry=False,
        )

    async def _record_failure(
        self,
        event: PendingOutboxEvent,
        timestamp: int,
        error_code: str,
        *,
        retry: bool = True,
    ) -> str:
        attempts = event.attempts + 1
        failed = not retry or attempts >= len(RETRY_DELAYS_SECONDS)
        available_at = timestamp if failed else timestamp + RETRY_DELAYS_SECONDS[attempts - 1]
        await self.store.record_delivery_failure(
            event.id,
            attempts,
            available_at,
            error_code,
            failed,
        )
        return "failed" if failed else "retried"
