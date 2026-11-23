from uuid import UUID

import pytest

from product_engine.repositories.events import ClaimResult

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


@pytest.mark.asyncio
async def test_usage_service_module_exists() -> None:
    from product_engine.services.sales_usage import SalesUsageService

    assert SalesUsageService is not None


@pytest.mark.asyncio
async def test_usage_service_refuses_unconfigured_dependencies() -> None:
    from product_engine.services.sales_usage import SalesUsageService

    with pytest.raises(RuntimeError, match="sales_usage_storage_not_configured"):
        await SalesUsageService().record(None, "a" * 64)


@pytest.mark.asyncio
async def test_usage_service_returns_store_result() -> None:
    from product_engine.services.sales_usage import SalesUsageService, UsageResult

    event = type("Event", (), {"tenant_id": TENANT_ID, "event_id": "event"})()

    class Inbox:
        async def claim_inbox(self, tenant_id, source_system, event_id, payload_sha256):
            assert tenant_id == TENANT_ID
            assert (source_system, event_id, payload_sha256) == ("sales", "event", "b" * 64)
            return ClaimResult("new")

    class Store:
        async def record(self, event, payload_sha256):
            assert event is event_value
            assert payload_sha256 == "b" * 64
            return UsageResult("accepted")

    event_value = event
    result = await SalesUsageService(inbox=Inbox(), store=Store()).record(event, "b" * 64)
    assert result.disposition == "accepted"


@pytest.mark.asyncio
async def test_usage_service_does_not_write_duplicate_event() -> None:
    from product_engine.services.sales_usage import SalesUsageService

    class Inbox:
        async def claim_inbox(self, tenant_id, source_system, event_id, payload_sha256):
            return ClaimResult("duplicate")

    class Store:
        async def record(self, event, payload_sha256):
            raise AssertionError("duplicate event must not create another usage row")

    event = type("Event", (), {"tenant_id": TENANT_ID, "event_id": "event"})()
    result = await SalesUsageService(inbox=Inbox(), store=Store()).record(event, "c" * 64)
    assert result.disposition == "duplicate"
