from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ProductCode = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
]


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    event_id: UUID
    occurred_at: AwareDatetime
    tenant_id: UUID

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: AwareDatetime) -> AwareDatetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("occurred_at_must_be_utc")
        return value
