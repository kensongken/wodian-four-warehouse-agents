from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.domain.enums import AssetStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User
from photo_organizer.repositories.asset_repository import AssetRepository
from photo_organizer.services.task_capacity_coordinator import (
    TaskCapacityCoordinator,
    TaskCapacityExceeded,
)


def _asset(user: User, task: ImportTask, local_id: str, digest: str) -> Asset:
    return Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        local_asset_id=local_id,
        filename=f"{local_id}.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256=digest,
        status=AssetStatus.UPLOADED,
    )


@pytest.mark.asyncio
async def test_coordinator_orders_local_id_then_digest_then_capacity(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The shared admission boundary must resolve retries and duplicates before limit."""
    user = User(id=uuid4(), phone_e164=f"+8613{uuid4().int % 10**8:08d}")
    task = ImportTask(id=uuid4(), user_id=user.id, name="Coordinator")
    local_retry = _asset(user, task, "source:retry", "a" * 64)
    digest_canonical = _asset(user, task, "source:canonical", "b" * 64)
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add(task)
        await session.flush()
        session.add_all([local_retry, digest_canonical])
        await session.commit()

    async with auth_session_factory() as session:
        repository = AssetRepository(session)
        coordinator = TaskCapacityCoordinator(repository, max_accepted_assets=2)

        retry = await coordinator.admit(
            _asset(user, task, local_retry.local_asset_id, "c" * 64)
        )
        duplicate = await coordinator.admit(
            _asset(user, task, "source:duplicate", digest_canonical.sha256)
        )
        with pytest.raises(TaskCapacityExceeded):
            await coordinator.admit(_asset(user, task, "source:unique", "d" * 64))
        await session.commit()

    assert retry.disposition == "existing"
    assert retry.asset.id == local_retry.id
    assert duplicate.disposition == "duplicate"
    assert duplicate.asset.canonical_asset_id == digest_canonical.id
    assert duplicate.asset.status == AssetStatus.SKIPPED_DUPLICATE
    assert duplicate.asset.blob_id is None
    async with auth_session_factory() as session:
        repository = AssetRepository(session)
        assert await repository.accepted_count(user.id, task.id) == 2
        assert (
            await session.scalar(
                select(func.count()).select_from(Asset).where(Asset.task_id == task.id)
            )
            == 3
        )


@pytest.mark.asyncio
async def test_coordinator_accepts_one_unique_canonical_below_capacity(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A unique proposal below the ceiling must become the counted canonical."""
    user = User(id=uuid4(), phone_e164=f"+8613{uuid4().int % 10**8:08d}")
    task = ImportTask(id=uuid4(), user_id=user.id, name="Available")
    proposed = _asset(user, task, "source:new", "e" * 64)
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add(task)
        await session.commit()

    async with auth_session_factory() as session:
        repository = AssetRepository(session)
        result = await TaskCapacityCoordinator(repository).admit(proposed)
        await session.commit()

    assert result.disposition == "accepted"
    assert result.asset.id == proposed.id
    async with auth_session_factory() as session:
        assert await AssetRepository(session).accepted_count(user.id, task.id) == 1
