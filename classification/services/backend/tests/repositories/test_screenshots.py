from uuid import UUID

import pytest

from product_engine.contracts.screenshots import ScreenshotImportedV1
from product_engine.repositories.screenshots import ScreenshotRepository


class Session:
    def __init__(self) -> None:
        self.added = []
        self.flushed = False

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushed = True


@pytest.mark.asyncio
async def test_screenshot_repository_creates_queued_import() -> None:
    event = ScreenshotImportedV1(
        schema_version=1,
        event_id="11111111-1111-4111-8111-111111111111",
        occurred_at="2026-08-15T08:00:00Z",
        tenant_id="22222222-2222-4222-8222-222222222222",
        screenshot_import_id="33333333-3333-4333-8333-333333333333",
        object_key="tenants/22222222-2222-4222-8222-222222222222/screenshots/2026/08/a.png",
        sha256="c" * 64,
        mime_type="image/png",
        byte_size=12,
        session_id="44444444-4444-4444-8444-444444444444",
        product_code="SP-00831",
    )
    session = Session()

    screenshot_id = await ScreenshotRepository(session).create(event)

    assert screenshot_id == UUID("33333333-3333-4333-8333-333333333333")
    assert session.flushed is True
    assert session.added[0].status == "queued"
    assert session.added[0].product_code == "SP-00831"
