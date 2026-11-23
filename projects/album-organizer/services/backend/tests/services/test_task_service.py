import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.domain.limits import MAX_TASK_ACCEPTED_ASSETS
from photo_organizer.models.asset import Asset
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User
from photo_organizer.services.task_service import TaskConflict, TaskService


@pytest_asyncio.fixture
async def service(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> TaskService:
    return TaskService(auth_session_factory)


@pytest_asyncio.fixture
async def task_with_uploaded_assets(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> ImportTask:
    user = User(id=uuid4(), phone_e164="+8613800010001")
    task = ImportTask(
        id=uuid4(),
        user_id=user.id,
        name="Uploaded assets",
        status=TaskStatus.UPLOADING,
    )
    asset = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        local_asset_id="asset-1",
        filename="asset-1.jpg",
        media_type="image/jpeg",
        byte_size=1,
        sha256="a" * 64,
        status=AssetStatus.UPLOADED,
    )
    async with auth_session_factory() as session:
        session.add_all([user, task, asset])
        await session.commit()
    return task


@pytest_asyncio.fixture
async def task_with_pending_asset(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> ImportTask:
    user = User(id=uuid4(), phone_e164="+8613800010002")
    task = ImportTask(
        id=uuid4(),
        user_id=user.id,
        name="Pending asset",
        status=TaskStatus.UPLOADING,
    )
    asset = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        local_asset_id="asset-1",
        filename="asset-1.jpg",
        media_type="image/jpeg",
        byte_size=1,
        sha256="b" * 64,
        status=AssetStatus.PENDING,
    )
    async with auth_session_factory() as session:
        session.add_all([user, task, asset])
        await session.commit()
    return task


@pytest.mark.asyncio
async def test_seal_complete_task_moves_to_ai_processing_and_writes_outbox(
    service: TaskService, task_with_uploaded_assets: ImportTask
) -> None:
    """Removing the atomic state handoff or event must fail sealing."""
    task = await service.seal(
        task_with_uploaded_assets.user_id, task_with_uploaded_assets.id
    )

    assert task.status == TaskStatus.AI_PROCESSING
    event = await service.outbox_for(task.id)
    assert event.topic == "ingestion.completed"
    assert event.payload["asset_count"] == task.total_assets


@pytest.mark.asyncio
async def test_seal_rejects_pending_assets(
    service: TaskService, task_with_pending_asset: ImportTask
) -> None:
    """Permitting incomplete accepted uploads must fail sealing."""
    with pytest.raises(TaskConflict, match="assets_not_terminal"):
        await service.seal(task_with_pending_asset.user_id, task_with_pending_asset.id)


@pytest.mark.asyncio
async def test_seal_rejects_an_empty_task(
    service: TaskService, auth_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Removing the lower asset-count guard must let an empty handoff through."""
    user = User(id=uuid4(), phone_e164="+8613800010003")
    task = ImportTask(
        id=uuid4(), user_id=user.id, name="Empty", status=TaskStatus.UPLOADING
    )
    async with auth_session_factory() as session:
        session.add_all([user, task])
        await session.commit()

    with pytest.raises(TaskConflict, match="invalid_asset_count"):
        await service.seal(user.id, task.id)


@pytest.mark.asyncio
async def test_seal_rejects_more_than_ten_thousand_uploaded_assets(
    service: TaskService, auth_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Allowing more than 10,000 accepted assets makes sealing exceed the task contract."""
    user = User(id=uuid4(), phone_e164="+8613800010004")
    task = ImportTask(
        id=uuid4(), user_id=user.id, name="Too many", status=TaskStatus.UPLOADING
    )
    assets = [
        Asset(
            id=uuid4(),
            user_id=user.id,
            task_id=task.id,
            local_asset_id=f"asset-{number}",
            filename=f"asset-{number}.jpg",
            media_type="image/jpeg",
            byte_size=1,
            sha256=f"{number:064x}",
            status=AssetStatus.UPLOADED,
        )
        for number in range(MAX_TASK_ACCEPTED_ASSETS + 1)
    ]
    async with auth_session_factory() as session:
        session.add_all([user, task, *assets])
        await session.commit()

    with pytest.raises(TaskConflict, match="invalid_asset_count"):
        await service.seal(user.id, task.id)


def test_task_capacity_contract_is_ten_thousand() -> None:
    """Changing the shared capacity constant would desynchronize service boundaries."""
    assert MAX_TASK_ACCEPTED_ASSETS == 10_000


@pytest.mark.asyncio
async def test_repeated_seal_is_idempotent_and_creates_one_event(
    service: TaskService,
    task_with_uploaded_assets: ImportTask,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dropping the AI-processing idempotency branch duplicates work and events."""
    first = await service.seal(
        task_with_uploaded_assets.user_id, task_with_uploaded_assets.id
    )
    second = await service.seal(
        task_with_uploaded_assets.user_id, task_with_uploaded_assets.id
    )
    async with auth_session_factory() as session:
        events = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == task_with_uploaded_assets.id)
        )

    assert first.status == second.status == TaskStatus.AI_PROCESSING
    assert events == 1


@pytest.mark.asyncio
async def test_seal_excludes_unsupported_terminal_assets_from_count(
    service: TaskService,
    task_with_uploaded_assets: ImportTask,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Counting unsupported files as accepted must change the handoff payload."""
    unsupported = Asset(
        id=uuid4(),
        user_id=task_with_uploaded_assets.user_id,
        task_id=task_with_uploaded_assets.id,
        local_asset_id="unsupported",
        filename="unsupported.raw",
        media_type=None,
        byte_size=None,
        sha256=None,
        status=AssetStatus.SKIPPED_UNSUPPORTED,
        unsupported_reason="unsupported_format",
    )
    async with auth_session_factory() as session:
        session.add(unsupported)
        await session.commit()

    task = await service.seal(
        task_with_uploaded_assets.user_id, task_with_uploaded_assets.id
    )

    assert (task.total_assets, task.uploaded_assets, task.unsupported_assets) == (
        1,
        1,
        1,
    )


@pytest.mark.asyncio
async def test_cancel_is_terminal_and_prevents_sealing(
    service: TaskService, task_with_uploaded_assets: ImportTask
) -> None:
    """Allowing a cancelled task to resume processing violates terminal cancellation."""
    cancelled = await service.cancel(
        task_with_uploaded_assets.user_id, task_with_uploaded_assets.id
    )

    assert cancelled.status == TaskStatus.CANCELLED
    with pytest.raises(TaskConflict, match="invalid_transition"):
        await service.seal(
            task_with_uploaded_assets.user_id, task_with_uploaded_assets.id
        )


@pytest.mark.asyncio
async def test_concurrent_seals_create_exactly_one_atomic_handoff_event(
    service: TaskService,
    task_with_uploaded_assets: ImportTask,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing the row lock allows simultaneous seal attempts to duplicate events."""
    first, second = await asyncio.gather(
        service.seal(task_with_uploaded_assets.user_id, task_with_uploaded_assets.id),
        service.seal(task_with_uploaded_assets.user_id, task_with_uploaded_assets.id),
    )
    async with auth_session_factory() as session:
        events = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == task_with_uploaded_assets.id)
        )

    assert (first.status, second.status, events) == (
        TaskStatus.AI_PROCESSING,
        TaskStatus.AI_PROCESSING,
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "downstream_status",
    [
        TaskStatus.WAITING_REVIEW,
        TaskStatus.COMPLETED,
        TaskStatus.PARTIAL_FAILURE,
    ],
)
async def test_seal_after_durable_handoff_event_is_idempotent_at_later_statuses(
    service: TaskService,
    task_with_uploaded_assets: ImportTask,
    auth_session_factory: async_sessionmaker[AsyncSession],
    downstream_status: TaskStatus,
) -> None:
    """Re-sealing after a downstream transition must preserve that state and event."""
    await service.seal(task_with_uploaded_assets.user_id, task_with_uploaded_assets.id)
    async with auth_session_factory() as session:
        await session.execute(
            update(ImportTask)
            .where(ImportTask.id == task_with_uploaded_assets.id)
            .values(status=downstream_status)
        )
        await session.commit()

    sealed_again = await service.seal(
        task_with_uploaded_assets.user_id, task_with_uploaded_assets.id
    )
    async with auth_session_factory() as session:
        event_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == task_with_uploaded_assets.id)
        )

    assert sealed_again.status == downstream_status
    assert event_count == 1
