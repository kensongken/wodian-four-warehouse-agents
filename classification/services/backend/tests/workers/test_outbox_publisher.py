from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from product_engine.services.outbox_relay import RelayResult


class FakeSession:
    pass


class FakeDatabase:
    def __init__(self) -> None:
        self.session_value = FakeSession()
        self.opened = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[FakeSession]:
        self.opened += 1
        yield self.session_value


@dataclass(frozen=True)
class FakeResponse:
    status_code: int


class FakeHttp:
    async def post(self, url: str, content: bytes, headers: dict[str, str]) -> FakeResponse:
        del url, content, headers
        return FakeResponse(204)


@pytest.mark.asyncio
async def test_publish_once_uses_transactional_session_and_returns_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from product_engine.workers import outbox_publisher

    captured: dict[str, object] = {}

    class FakeRepository:
        def __init__(self, session: object) -> None:
            captured["session"] = session

    class FakeRelay:
        def __init__(
            self,
            store: object,
            http: object,
            sales_webhook_url: str,
            signing_secret: bytes,
            *,
            now: object,
        ) -> None:
            captured.update(
                store=store,
                http=http,
                url=sales_webhook_url,
                secret=signing_secret,
                now=now,
            )

        async def publish_batch(self, limit: int = 100, *, topic: str | None = None) -> RelayResult:
            captured["limit"] = limit
            captured["topic"] = topic
            return RelayResult(delivered=2, retried=1, failed=0)

    monkeypatch.setattr(outbox_publisher, "EventRepository", FakeRepository)
    monkeypatch.setattr(outbox_publisher, "OutboxRelay", FakeRelay)
    database = FakeDatabase()
    http = FakeHttp()

    result = await outbox_publisher.publish_once(
        database,
        http,
        "https://sales.example.test/api/integrations/product-image-engine/events",
        b"synthetic-secret",
        now=lambda: 1_786_780_850,
        limit=25,
    )

    assert result == RelayResult(delivered=2, retried=1, failed=0)
    assert database.opened == 1
    assert captured["session"] is database.session_value
    assert captured["http"] is http
    assert captured["limit"] == 25
    assert captured["topic"] == "sales.strategy.input.updated.v1"


def test_render_result_is_stable_machine_readable_json() -> None:
    from product_engine.workers.outbox_publisher import render_result

    assert render_result(RelayResult(delivered=2, retried=1, failed=0)) == (
        '{"delivered":2,"failed":0,"retried":1}'
    )


@pytest.mark.asyncio
async def test_httpx_poster_translates_transport_failure_for_relay_retry() -> None:
    from product_engine.workers.outbox_publisher import HttpxPoster

    async def disconnect(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("synthetic disconnect", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(disconnect)) as client:
        with pytest.raises(OSError, match="sales_webhook_transport_error"):
            await HttpxPoster(client).post(
                "https://sales.example.test/api/integrations/product-image-engine/events",
                b"{}",
                {"Content-Type": "application/json"},
            )


@pytest.mark.asyncio
async def test_run_does_not_route_local_delivery_through_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from product_engine.workers import outbox_publisher

    captured: dict[str, object] = {}

    class FakeSecret:
        def get_secret_value(self) -> str:
            return "synthetic-sales-delivery-secret-with-32-bytes"

    class FakeSettings:
        database_url = "postgresql+asyncpg://unused"
        sales_webhook_url = "http://127.0.0.1:3002/events"
        sales_signing_secret = FakeSecret()

    class FakeRuntimeDatabase:
        def __init__(self, url: str) -> None:
            captured["database_url"] = url

        async def dispose(self) -> None:
            captured["disposed"] = True

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

    async def fake_publish_once(*args: object, **kwargs: object) -> RelayResult:
        del args, kwargs
        return RelayResult(delivered=1)

    monkeypatch.setattr(outbox_publisher, "Settings", FakeSettings)
    monkeypatch.setattr(outbox_publisher, "Database", FakeRuntimeDatabase)
    monkeypatch.setattr(outbox_publisher.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(outbox_publisher, "publish_once", fake_publish_once)

    assert await outbox_publisher.run(1) == RelayResult(delivered=1)
    assert captured["trust_env"] is False
    assert captured["disposed"] is True
