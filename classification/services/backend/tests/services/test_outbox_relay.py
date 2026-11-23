from dataclasses import dataclass
from uuid import UUID

import pytest

from product_engine.security.signatures import verify_request_signature

NOW = 1_786_780_850
SECRET = b"sales-webhook-secret"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


@dataclass(frozen=True)
class PendingEvent:
    id: UUID
    tenant_id: UUID
    topic: str
    payload: dict[str, object]
    attempts: int = 0


class FakeOutboxStore:
    def __init__(self, events: list[PendingEvent]) -> None:
        self.events = events
        self.delivered: list[UUID] = []
        self.retries: list[tuple[UUID, int, int, str, bool]] = []

    async def claim_pending(
        self, limit: int, now: int, *, topic: str | None = None
    ) -> list[PendingEvent]:
        assert limit == 10
        assert now == NOW
        assert topic is None
        return self.events

    async def mark_delivered(self, event_id: UUID, delivered_at: int) -> None:
        assert delivered_at == NOW
        self.delivered.append(event_id)

    async def record_delivery_failure(
        self,
        event_id: UUID,
        attempts: int,
        available_at: int,
        error_code: str,
        failed: bool,
    ) -> None:
        self.retries.append((event_id, attempts, available_at, error_code, failed))


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeHttp:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []

    async def post(self, url: str, content: bytes, headers: dict[str, str]) -> FakeResponse:
        self.requests.append((url, content, headers))
        return self.responses.pop(0)


def event(*, attempts: int = 0) -> PendingEvent:
    return PendingEvent(
        id=UUID("11111111-1111-4111-8111-111111111111"),
        tenant_id=TENANT_ID,
        topic="product.materials.updated.v1",
        payload={"schema_version": 1, "product_code": "SP-00831"},
        attempts=attempts,
    )


@pytest.mark.asyncio
async def test_relay_signs_and_marks_successful_event_delivered() -> None:
    from product_engine.services.outbox_relay import OutboxRelay

    store = FakeOutboxStore([event()])
    http = FakeHttp([FakeResponse(204)])

    result = await OutboxRelay(
        store,
        http,
        "https://sales.example.test/hooks/materials",
        SECRET,
        now=lambda: NOW,
    ).publish_batch(limit=10)

    assert result.delivered == 1
    assert result.retried == 0
    assert store.delivered == [event().id]
    url, content, headers = http.requests[0]
    assert url == "https://sales.example.test/hooks/materials"
    assert headers["X-Source-System"] == "product-image-engine"
    assert headers["X-Event-Topic"] == "product.materials.updated.v1"
    assert headers["X-Outbox-Event-Id"] == str(event().id)
    verify_request_signature(
        secret=SECRET,
        timestamp=NOW,
        now=NOW,
        tolerance_seconds=300,
        source_system="product-image-engine",
        tenant_id=TENANT_ID,
        method="POST",
        path="/hooks/materials",
        body=content,
        supplied_signature=headers["X-Signature"],
    )


@pytest.mark.asyncio
async def test_relay_retries_transient_failure_after_thirty_seconds() -> None:
    from product_engine.services.outbox_relay import OutboxRelay

    store = FakeOutboxStore([event()])
    http = FakeHttp([FakeResponse(503)])

    result = await OutboxRelay(
        store,
        http,
        "https://sales.example.test/hooks/materials",
        SECRET,
        now=lambda: NOW,
    ).publish_batch(limit=10)

    assert result.delivered == 0
    assert result.retried == 1
    assert store.retries == [(event().id, 1, NOW + 30, "http_503", False)]


@pytest.mark.asyncio
async def test_relay_marks_fifth_transient_failure_as_failed() -> None:
    from product_engine.services.outbox_relay import OutboxRelay

    store = FakeOutboxStore([event(attempts=4)])
    http = FakeHttp([FakeResponse(429)])

    result = await OutboxRelay(
        store,
        http,
        "https://sales.example.test/hooks/materials",
        SECRET,
        now=lambda: NOW,
    ).publish_batch(limit=10)

    assert result.failed == 1
    assert store.retries == [(event().id, 5, NOW, "http_429", True)]


@pytest.mark.asyncio
async def test_relay_rejects_invalid_sales_strategy_event_before_delivery() -> None:
    from product_engine.services.outbox_relay import OutboxRelay

    invalid = PendingEvent(
        id=event().id,
        tenant_id=TENANT_ID,
        topic="sales.strategy.input.updated.v1",
        payload={
            "schema_version": 1,
            "tenant_id": str(TENANT_ID),
            "product_code": "SYNTHETIC-001",
            "strategy_input_version": 1,
            "status": "sales_ready",
        },
    )
    store = FakeOutboxStore([invalid])
    http = FakeHttp([])

    result = await OutboxRelay(
        store,
        http,
        "https://sales.example.test/hooks/materials",
        SECRET,
        now=lambda: NOW,
    ).publish_batch(limit=10)

    assert result.failed == 1
    assert http.requests == []
    assert store.retries == [(invalid.id, 1, NOW, "invalid_sales_strategy_event", True)]
