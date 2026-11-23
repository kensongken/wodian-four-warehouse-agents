from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.api.dependencies import get_current_user
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User
from photo_organizer.schemas.tasks import (
    CreateTaskRequest,
    TaskListResponse,
    TaskSummary,
)
from photo_organizer.services.task_service import (
    TaskConflict,
    TaskNotFound,
    TaskService,
)

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


def get_task_service(request: Request) -> TaskService:
    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    return TaskService(
        session_factory,
        cleanup_processor=request.app.state.upload_service.process_cleanup_jobs,
    )


@router.post("", response_model=TaskSummary, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: CreateTaskRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    service: TaskService = Depends(get_task_service),  # noqa: B008
) -> TaskSummary:
    return TaskSummary.model_validate(await service.create(user.id, body.name))


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    limit: int = Query(default=20, ge=1, le=50),
    before: str | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
    service: TaskService = Depends(get_task_service),  # noqa: B008
) -> TaskListResponse:
    cursor = _parse_cursor(before) if before is not None else None
    tasks = await service.list(user.id, limit=limit, before=cursor)
    return TaskListResponse(
        items=[TaskSummary.model_validate(task) for task in tasks],
        next_before=_cursor_for(tasks[-1]) if len(tasks) == limit else None,
    )


@router.get("/{task_id}", response_model=TaskSummary)
async def get_task(
    task_id: UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    service: TaskService = Depends(get_task_service),  # noqa: B008
) -> TaskSummary:
    return TaskSummary.model_validate(await _get_task(service, user, task_id))


@router.post("/{task_id}/seal", response_model=TaskSummary)
async def seal_task(
    task_id: UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    service: TaskService = Depends(get_task_service),  # noqa: B008
) -> TaskSummary:
    try:
        return TaskSummary.model_validate(await service.seal(user.id, task_id))
    except TaskNotFound as error:
        raise _not_found(error) from error
    except TaskConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error


@router.post("/{task_id}/cancel", response_model=TaskSummary)
async def cancel_task(
    task_id: UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    service: TaskService = Depends(get_task_service),  # noqa: B008
) -> TaskSummary:
    try:
        return TaskSummary.model_validate(await service.cancel(user.id, task_id))
    except TaskNotFound as error:
        raise _not_found(error) from error
    except TaskConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error


async def _get_task(service: TaskService, user: User, task_id: UUID) -> ImportTask:
    try:
        return await service.get(user.id, task_id)
    except TaskNotFound as error:
        raise _not_found(error) from error


def _not_found(error: TaskNotFound) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def _cursor_for(task: ImportTask) -> str:
    return f"{task.created_at.isoformat().replace('+00:00', 'Z')},{task.id}"


def _parse_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        timestamp, task_id = value.split(",", maxsplit=1)
        parsed_timestamp = datetime.fromisoformat(timestamp)
        if parsed_timestamp.tzinfo is None:
            raise ValueError
        return parsed_timestamp, UUID(task_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid_cursor",
        ) from error
