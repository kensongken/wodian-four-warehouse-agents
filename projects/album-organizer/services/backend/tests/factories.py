from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User


async def create_user(session: AsyncSession, *, user_id: UUID, phone_e164: str) -> User:
    user = User(id=user_id, phone_e164=phone_e164)
    session.add(user)
    await session.flush()
    return user


async def create_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    user_id: UUID,
    name: str,
    status: TaskStatus = TaskStatus.WAITING_UPLOAD,
) -> ImportTask:
    task = ImportTask(id=task_id, user_id=user_id, name=name, status=status)
    session.add(task)
    await session.flush()
    return task


async def create_blob(
    session: AsyncSession,
    *,
    blob_id: UUID,
    user_id: UUID,
    sha256: str,
    byte_size: int,
    object_key: str,
    is_ready: bool = False,
) -> StoredBlob:
    blob = StoredBlob(
        id=blob_id,
        user_id=user_id,
        sha256=sha256,
        byte_size=byte_size,
        object_key=object_key,
        is_ready=is_ready,
    )
    session.add(blob)
    await session.flush()
    return blob


async def create_asset(
    session: AsyncSession,
    *,
    asset_id: UUID,
    user_id: UUID,
    task_id: UUID,
    local_asset_id: str,
    filename: str,
    media_type: str | None,
    byte_size: int | None,
    sha256: str | None,
    status: AssetStatus,
    blob_id: UUID | None = None,
    unsupported_reason: str | None = None,
) -> Asset:
    asset = Asset(
        id=asset_id,
        user_id=user_id,
        task_id=task_id,
        blob_id=blob_id,
        local_asset_id=local_asset_id,
        filename=filename,
        media_type=media_type,
        byte_size=byte_size,
        sha256=sha256,
        status=status,
        unsupported_reason=unsupported_reason,
    )
    session.add(asset)
    await session.flush()
    return asset
