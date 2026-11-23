from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from photo_organizer.domain.enums import AssetStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob

COUNTED_ASSET_STATUSES = (
    AssetStatus.PENDING,
    AssetStatus.UPLOADING,
    AssetStatus.UPLOADED,
    AssetStatus.FAILED,
)


class AssetRepository:
    """Tenant-scoped asset and blob persistence operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, user_id: UUID, asset_id: UUID, *, lock: bool = False
    ) -> Asset | None:
        statement = select(Asset).where(Asset.user_id == user_id, Asset.id == asset_id)
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def for_task_local(
        self, user_id: UUID, task_id: UUID, local_asset_id: str
    ) -> Asset | None:
        return await self._session.scalar(
            select(Asset).where(
                Asset.user_id == user_id,
                Asset.task_id == task_id,
                Asset.local_asset_id == local_asset_id,
            )
        )

    async def blob(
        self, user_id: UUID, blob_id: UUID, *, lock: bool = False
    ) -> StoredBlob | None:
        statement = select(StoredBlob).where(
            StoredBlob.user_id == user_id, StoredBlob.id == blob_id
        )
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def ready_blob(
        self, user_id: UUID, sha256: str, byte_size: int
    ) -> StoredBlob | None:
        return await self._session.scalar(
            select(StoredBlob).where(
                StoredBlob.user_id == user_id,
                StoredBlob.sha256 == sha256,
                StoredBlob.byte_size == byte_size,
                StoredBlob.is_ready.is_(True),
            )
        )

    async def for_task_content(
        self, user_id: UUID, task_id: UUID, sha256: str, byte_size: int
    ) -> Asset | None:
        """Return the canonical accepted membership for task-local content."""
        return await self._session.scalar(
            select(Asset)
            .where(
                Asset.user_id == user_id,
                Asset.task_id == task_id,
                Asset.sha256 == sha256,
                Asset.byte_size == byte_size,
                Asset.canonical_asset_id.is_(None),
                Asset.status.in_(COUNTED_ASSET_STATUSES),
            )
            .order_by(Asset.created_at, Asset.id)
            .limit(1)
        )

    async def lock_digest(self, user_id: UUID, sha256: str, byte_size: int) -> None:
        """Serialize ready-blob winner selection across every application process."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{user_id}:{sha256}:{byte_size}"},
        )

    async def accepted_count(self, user_id: UUID, task_id: UUID) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(Asset)
                .where(
                    Asset.user_id == user_id,
                    Asset.task_id == task_id,
                    Asset.status.in_(COUNTED_ASSET_STATUSES),
                )
            )
            or 0
        )

    async def counts(self, user_id: UUID, task_id: UUID) -> tuple[int, int, int, int]:
        rows = await self._session.execute(
            select(Asset.status, func.count())
            .where(Asset.user_id == user_id, Asset.task_id == task_id)
            .group_by(Asset.status)
        )
        by_status = {status: count for status, count in rows.tuples()}
        uploaded = int(by_status.get(AssetStatus.UPLOADED, 0))
        failed = int(by_status.get(AssetStatus.FAILED, 0))
        unsupported = int(by_status.get(AssetStatus.SKIPPED_UNSUPPORTED, 0))
        total = sum(
            int(by_status.get(status, 0))
            for status in (
                AssetStatus.PENDING,
                AssetStatus.UPLOADING,
                AssetStatus.UPLOADED,
                AssetStatus.FAILED,
            )
        )
        return total, uploaded, failed, unsupported

    async def add(self, entity: Asset | StoredBlob) -> None:
        self._session.add(entity)
        await self._session.flush()
