from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints

from photo_organizer.domain.enums import TaskStatus

TaskName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80, strict=True),
]


class CreateTaskRequest(BaseModel):
    name: TaskName


class TaskSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: TaskStatus
    total_assets: int
    uploaded_assets: int
    failed_assets: int
    unsupported_assets: int
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskSummary]
    next_before: str | None
