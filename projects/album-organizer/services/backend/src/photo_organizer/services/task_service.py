from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.domain.limits import MAX_TASK_ACCEPTED_ASSETS
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.cleanup_job import StorageCleanupJob
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask
from photo_organizer.repositories.task_repository import TaskRepository
from photo_organizer.repositories.unit_of_work import UnitOfWork

ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.WAITING_UPLOAD: {TaskStatus.UPLOADING, TaskStatus.CANCELLED},
    TaskStatus.UPLOADING: {
        TaskStatus.PAUSED,
        TaskStatus.AI_PROCESSING,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PAUSED: {TaskStatus.UPLOADING, TaskStatus.CANCELLED},
    TaskStatus.AI_PROCESSING: {
        TaskStatus.WAITING_REVIEW,
        TaskStatus.COMPLETED,
        TaskStatus.PARTIAL_FAILURE,
    },
    TaskStatus.PARTIAL_FAILURE: {
        TaskStatus.AI_PROCESSING,
        TaskStatus.WAITING_REVIEW,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_REVIEW: {TaskStatus.COMPLETED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.CANCELLED: set(),
}


class TaskError(Exception):
    """Base exception for an expected task lifecycle failure."""


class TaskNotFound(TaskError):
    """The requested tenant-scoped task does not exist."""


class TaskConflict(TaskError):
    """The requested task lifecycle transition is not currently possible."""


class TaskService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cleanup_processor: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cleanup_processor = cleanup_processor

    async def create(self, user_id: UUID, name: str) -> ImportTask:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            task = ImportTask(user_id=user_id, name=name)
            return await TaskRepository(unit_of_work.session).add(task)

    async def get(self, user_id: UUID, task_id: UUID) -> ImportTask:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            task = await TaskRepository(unit_of_work.session).get(user_id, task_id)
            if task is None:
                raise TaskNotFound("task_not_found")
            return task

    async def list(
        self,
        user_id: UUID,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[ImportTask]:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            return await TaskRepository(unit_of_work.session).list(
                user_id, limit=limit, before=before
            )

    async def seal(self, user_id: UUID, task_id: UUID) -> ImportTask:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = TaskRepository(unit_of_work.session)
            task = await repository.get_for_update(user_id, task_id)
            if task is None:
                raise TaskNotFound("task_not_found")
            if await repository.outbox_for(task.id) is not None:
                return task
            self._transition(task, TaskStatus.AI_PROCESSING)
            counts = await self._asset_counts(unit_of_work.session, task.id, user_id)
            self._validate_seal_counts(counts)
            task.total_assets = counts.accepted
            task.uploaded_assets = counts.uploaded
            task.failed_assets = counts.failed
            task.unsupported_assets = counts.unsupported
            repository.add_ingestion_completed(task)
            await unit_of_work.session.flush()
            await unit_of_work.session.refresh(task)
            return task

    async def cancel(self, user_id: UUID, task_id: UUID) -> ImportTask:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            task = await TaskRepository(unit_of_work.session).get_for_update(
                user_id, task_id
            )
            if task is None:
                raise TaskNotFound("task_not_found")
            if task.status == TaskStatus.CANCELLED:
                result = task
            else:
                self._transition(task, TaskStatus.CANCELLED)
                # Task row is the lifecycle gate used by upload operations.  Once
                # this transition commits, no new signing or completion work may
                # begin; all observable active multipart uploads receive durable
                # idempotent abort work before releasing it.
                active = await unit_of_work.session.execute(
                    select(Asset, StoredBlob)
                    .join(StoredBlob, Asset.blob_id == StoredBlob.id)
                    .where(
                        Asset.user_id == user_id,
                        Asset.task_id == task_id,
                        Asset.status.in_((AssetStatus.PENDING, AssetStatus.UPLOADING)),
                        StoredBlob.provider_upload_id.is_not(None),
                    )
                    .with_for_update()
                )
                for asset, blob in active.tuples():
                    blob.cleanup_pending = True
                    unit_of_work.session.add(
                        StorageCleanupJob(
                            user_id=user_id,
                            blob_id=blob.id,
                            operation="abort_multipart",
                            object_key=blob.object_key,
                            provider_upload_id=blob.provider_upload_id,
                        )
                    )
                await unit_of_work.session.flush()
                await unit_of_work.session.refresh(task)
                result = task
        if self._cleanup_processor is not None:
            try:
                await self._cleanup_processor(user_id, limit=25)
            except Exception:  # noqa: BLE001, S110 - durable jobs remain for scheduled drain.
                pass
        return result

    async def outbox_for(self, task_id: UUID) -> OutboxEvent:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            event = await TaskRepository(unit_of_work.session).outbox_for(task_id)
            if event is None:
                raise TaskNotFound("outbox_event_not_found")
            return event

    @staticmethod
    def _transition(task: ImportTask, target: TaskStatus) -> None:
        if target not in ALLOWED_TRANSITIONS[task.status]:
            raise TaskConflict("invalid_transition")
        task.status = target

    async def _asset_counts(
        self, session: AsyncSession, task_id: UUID, user_id: UUID
    ) -> "AssetCounts":
        rows = await session.execute(
            select(Asset.status, func.count())
            .where(Asset.task_id == task_id, Asset.user_id == user_id)
            .group_by(Asset.status)
        )
        by_status = {status: count for status, count in rows.tuples()}
        return AssetCounts(
            uploaded=by_status.get(AssetStatus.UPLOADED, 0),
            failed=by_status.get(AssetStatus.FAILED, 0),
            unsupported=by_status.get(AssetStatus.SKIPPED_UNSUPPORTED, 0),
            non_terminal=(
                by_status.get(AssetStatus.PENDING, 0)
                + by_status.get(AssetStatus.UPLOADING, 0)
            ),
        )

    @staticmethod
    def _validate_seal_counts(counts: "AssetCounts") -> None:
        if counts.non_terminal:
            raise TaskConflict("assets_not_terminal")
        if not 1 <= counts.accepted <= MAX_TASK_ACCEPTED_ASSETS:
            raise TaskConflict("invalid_asset_count")
        if counts.failed:
            raise TaskConflict("assets_not_uploaded")


class AssetCounts:
    def __init__(
        self, *, uploaded: int, failed: int, unsupported: int, non_terminal: int
    ) -> None:
        self.uploaded = uploaded
        self.failed = failed
        self.unsupported = unsupported
        self.non_terminal = non_terminal

    @property
    def accepted(self) -> int:
        return self.uploaded + self.failed + self.non_terminal
