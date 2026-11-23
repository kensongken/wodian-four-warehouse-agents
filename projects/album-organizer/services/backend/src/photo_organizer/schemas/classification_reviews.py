from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from photo_organizer.categorization.contracts import (
    Level1Category,
    Level2Category,
    ProductSourceType,
    RankedCandidate,
    StableId,
)

ReviewStatus = Literal["open", "resolved"]
UnevaluatedClaim = Literal[
    "authenticity",
    "origin",
    "quality",
    "warehouse_acceptance",
]


class ConfirmClassificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level_1: Level1Category
    level_2: Level2Category


class ClassificationReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    opc_id: StableId
    classification_id: UUID
    album_product_id: StableId
    source_type: ProductSourceType
    input_snapshot_version: Annotated[int, Field(ge=1)]
    classification_version: Annotated[int, Field(ge=1)]
    candidates: Annotated[
        tuple[RankedCandidate, ...], Field(min_length=1, max_length=3)
    ]
    status: ReviewStatus
    confirmed_level_1: Level1Category | None
    confirmed_level_2: Level2Category | None
    confirmed_by: StableId | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime
    claims_not_evaluated: tuple[UnevaluatedClaim, ...]
    mall_product_id: None
    bead_id: None


class ClassificationReviewListResponse(BaseModel):
    items: list[ClassificationReviewResponse]
    next_before: str | None
