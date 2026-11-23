from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask


class TaskRepository:
    """Tenant-scoped persistence operations for import tasks."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: ImportTask) -> ImportTask:
        self._session.add(task)
        await self._session.flush()
        return task

    async def get(self, user_id: UUID, task_id: UUID) -> ImportTask | None:
        return await self._session.scalar(self._task_for_user(user_id, task_id))

    async def get_for_update(self, user_id: UUID, task_id: UUID) -> ImportTask | None:
        statement = self._task_for_user(user_id, task_id).with_for_update()
        return await self._session.scalar(statement)

    async def list(
        self,
        user_id: UUID,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[ImportTask]:
        statement = select(ImportTask).where(ImportTask.user_id == user_id)
        if before is not None:
            created_at, task_id = before
            statement = statement.where(
                or_(
                    ImportTask.created_at < created_at,
                    and_(ImportTask.created_at == created_at, ImportTask.id < task_id),
                )
            )
        statement = statement.order_by(
            ImportTask.created_at.desc(), ImportTask.id.desc()
        ).limit(limit)
        return list((await self._session.scalars(statement)).all())

    async def outbox_for(self, task_id: UUID) -> OutboxEvent | None:
        statement = (
            select(OutboxEvent)
            .where(
                OutboxEvent.aggregate_id == task_id,
                OutboxEvent.topic == "ingestion.completed",
            )
            .order_by(OutboxEvent.sequence)
        )
        return await self._session.scalar(statement)

    def add_ingestion_completed(self, task: ImportTask) -> OutboxEvent:
        event = OutboxEvent(
            topic="ingestion.completed",
            aggregate_id=task.id,
            sequence=1,
            payload={
                "task_id": str(task.id),
                "user_id": str(task.user_id),
                "asset_count": task.total_assets,
            },
        )
        self._session.add(event)
        return event

    @staticmethod
    def _task_for_user(user_id: UUID, task_id: UUID) -> Select[tuple[ImportTask]]:
        return select(ImportTask).where(
            ImportTask.id == task_id, ImportTask.user_id == user_id
        )
