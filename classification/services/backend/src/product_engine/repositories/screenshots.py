from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.contracts.screenshots import ScreenshotImportedV1
from product_engine.models.screenshots import ScreenshotImport


class ScreenshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find_by_sha256(self, tenant_id: UUID, sha256: str) -> UUID | None:
        return (
            await self.session.execute(
                select(ScreenshotImport.id).where(
                    ScreenshotImport.tenant_id == tenant_id,
                    ScreenshotImport.sha256 == sha256,
                )
            )
        ).scalar_one_or_none()

    async def find_by_event_id(self, tenant_id: UUID, event_id: UUID) -> UUID | None:
        return (
            await self.session.execute(
                select(ScreenshotImport.id).where(
                    ScreenshotImport.tenant_id == tenant_id,
                    ScreenshotImport.event_id == event_id,
                )
            )
        ).scalar_one_or_none()

    async def create(self, event: ScreenshotImportedV1) -> UUID:
        screenshot_id = event.screenshot_import_id
        self.session.add(
            ScreenshotImport(
                id=screenshot_id or uuid4(),
                tenant_id=event.tenant_id,
                event_id=event.event_id,
                object_key=event.object_key,
                sha256=event.sha256,
                mime_type=event.mime_type,
                byte_size=event.byte_size,
                session_id=event.session_id,
                product_code=event.product_code,
                status="queued",
            )
        )
        await self.session.flush()
        return screenshot_id
