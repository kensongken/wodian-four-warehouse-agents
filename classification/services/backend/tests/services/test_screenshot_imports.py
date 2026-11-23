from uuid import UUID

import pytest

from product_engine.contracts.screenshots import ScreenshotImportedV1
from product_engine.domain.errors import MaterialUsageReferenceNotFound, ObjectValidationFailed
from product_engine.repositories.events import ClaimResult
from product_engine.services.screenshot_imports import (
    ScreenshotImportResult,
    ScreenshotImportService,
)
from product_engine.storage.protocol import StoredObject

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
SCREENSHOT_ID = UUID("33333333-3333-4333-8333-333333333333")


def event() -> ScreenshotImportedV1:
    return ScreenshotImportedV1(
        schema_version=1,
        event_id="11111111-1111-4111-8111-111111111111",
        occurred_at="2026-08-15T08:00:00Z",
        tenant_id=TENANT_ID,
        screenshot_import_id=SCREENSHOT_ID,
        object_key=f"tenants/{TENANT_ID}/screenshots/2026/08/test.png",
        sha256="c" * 64,
        mime_type="image/png",
        byte_size=640_000,
    )


class Storage:
    async def inspect(self, object_key):
        return StoredObject(object_key, "image/png", 640_000, "c" * 64)


class Inbox:
    async def claim_inbox(self, tenant_id, source_system, event_id, payload_sha256):
        assert tenant_id == TENANT_ID
        assert source_system == "sales"
        return ClaimResult("new")


class Store:
    def __init__(self, existing=None, event_existing=None):
        self.existing = existing
        self.event_existing = event_existing
        self.created = []

    async def find_by_sha256(self, tenant_id, sha256):
        return self.existing

    async def find_by_event_id(self, tenant_id, event_id):
        return self.event_existing

    async def create(self, screenshot_event):
        self.created.append(screenshot_event)
        return screenshot_event.screenshot_import_id


class Outbox:
    def __init__(self):
        self.messages = []

    async def append_outbox(self, tenant_id, topic, aggregate_key, payload):
        self.messages.append((tenant_id, topic, aggregate_key, payload))
        return UUID("44444444-4444-4444-8444-444444444444")


@pytest.mark.asyncio
async def test_finalize_writes_screenshot_and_outbox_once() -> None:
    store = Store()
    outbox = Outbox()
    result = await ScreenshotImportService(Storage(), store, Inbox(), outbox).finalize(
        event(), "a" * 64
    )

    assert result.disposition == "accepted"
    assert store.created == [event()]
    assert outbox.messages[0][1:3] == ("screenshot.imported.v1", str(SCREENSHOT_ID))


@pytest.mark.asyncio
async def test_finalize_deduplicates_existing_tenant_sha_without_outbox() -> None:
    store = Store(existing=SCREENSHOT_ID)
    outbox = Outbox()
    result = await ScreenshotImportService(Storage(), store, Inbox(), outbox).finalize(
        event(), "a" * 64
    )

    assert result.disposition == "duplicate"
    assert store.created == []
    assert outbox.messages == []


@pytest.mark.asyncio
async def test_finalize_rejects_object_outside_current_tenant_prefix() -> None:
    other_tenant = UUID("55555555-5555-4555-8555-555555555555")
    invalid = event().model_copy(
        update={"object_key": f"tenants/{other_tenant}/screenshots/2026/08/test.png"}
    )

    with pytest.raises(ObjectValidationFailed, match="object_outside_tenant_prefix"):
        await ScreenshotImportService(Storage(), Store(), Inbox(), Outbox()).finalize(
            invalid, "a" * 64
        )


@pytest.mark.asyncio
async def test_finalize_replayed_event_returns_existing_id_without_outbox() -> None:
    class DuplicateInbox:
        async def claim_inbox(self, tenant_id, source_system, event_id, payload_sha256):
            return ClaimResult("duplicate")

    store = Store(event_existing=SCREENSHOT_ID)
    outbox = Outbox()
    result = await ScreenshotImportService(Storage(), store, DuplicateInbox(), outbox).finalize(
        event(), "a" * 64
    )

    assert result.disposition == "duplicate"
    assert store.created == []
    assert outbox.messages == []


@pytest.mark.asyncio
async def test_finalize_replays_new_event_deduplicated_by_tenant_sha() -> None:
    class DuplicateInbox:
        async def claim_inbox(self, tenant_id, source_system, event_id, payload_sha256):
            return ClaimResult("duplicate")

    store = Store(existing=SCREENSHOT_ID)
    outbox = Outbox()

    result = await ScreenshotImportService(Storage(), store, DuplicateInbox(), outbox).finalize(
        event(), "a" * 64
    )

    assert result == ScreenshotImportResult(SCREENSHOT_ID, "duplicate")
    assert store.created == []
    assert outbox.messages == []


@pytest.mark.asyncio
async def test_finalize_rejects_product_code_outside_tenant() -> None:
    class Products:
        async def exists(self, tenant_id, product_code):
            assert tenant_id == TENANT_ID
            assert product_code == "SP-OTHER-TENANT"
            return False

    screenshot_event = event().model_copy(update={"product_code": "SP-OTHER-TENANT"})
    outbox = Outbox()
    with pytest.raises(MaterialUsageReferenceNotFound, match="product_not_found"):
        await ScreenshotImportService(
            Storage(), Store(), Inbox(), outbox, products=Products()
        ).finalize(screenshot_event, "a" * 64)
    assert outbox.messages == []
