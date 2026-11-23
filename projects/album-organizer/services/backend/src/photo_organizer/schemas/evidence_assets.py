from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

EvidenceMediaType = Literal[
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/heif",
]


class EvidenceAssetRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_asset_id: UUID


class EvidenceAssetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["opc-evidence-asset/1.0"]
    evidence_asset_id: UUID
    opc_id: Annotated[
        str,
        Field(
            min_length=3,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ]
    source_asset_id: UUID
    storage_ref: Annotated[str, Field(min_length=12, max_length=512)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    byte_size: Annotated[int, Field(gt=0, le=104_857_600)]
    media_type: EvidenceMediaType
    captured_at: datetime | None
    imported_at: datetime
