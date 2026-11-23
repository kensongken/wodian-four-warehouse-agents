from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from photo_organizer.domain.enums import AssetStatus


class AssetInitRequest(BaseModel):
    binding_id: UUID | None = None
    candidate_id: str | None = Field(
        default=None,
        pattern=r"^opc-controlled-upload-candidate:[0-9a-f]{64}$",
    )
    local_asset_id: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"enum": ["image/heic", "image/jpeg", "image/png"]},
    )
    byte_size: int = Field(gt=0, json_schema_extra={"maximum": 104_857_600})
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime | None = None
    retry: bool = False

    @model_validator(mode="after")
    def binding_authority_must_be_complete(self) -> Self:
        if (self.binding_id is None) != (self.candidate_id is None):
            raise ValueError("binding_id and candidate_id must be supplied together")
        return self


class AssetInitResponse(BaseModel):
    asset_id: UUID
    already_uploaded: bool
    provider_upload_id: str | None
    part_size: int = 8 * 1024 * 1024
    completed_parts: list[int]
    disposition: Literal["accepted", "duplicate"] = "accepted"
    canonical_asset_id: UUID | None = None


class CompletedPart(BaseModel):
    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=256)


class CompleteAssetRequest(BaseModel):
    parts: list[CompletedPart] = Field(min_length=1, max_length=10_000)


class SignPartRequest(BaseModel):
    part_number: int = Field(ge=1, le=10_000)


class SignPartResponse(BaseModel):
    url: str
    expires_seconds: int


class FailAssetRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1024)


class UnsupportedAssetRequest(BaseModel):
    local_asset_id: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str | None = Field(default=None, max_length=128)
    byte_size: int | None = Field(default=None, ge=0)
    reason: Literal["unsupported_media_type", "image_too_large", "read_failure"]


class AssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    local_asset_id: str
    filename: str
    media_type: str | None
    byte_size: int | None
    sha256: str | None
    status: AssetStatus
    unsupported_reason: str | None
    failure_code: str | None
    failure_message: str | None
    canonical_asset_id: UUID | None
