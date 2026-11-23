from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from photo_organizer.categorization.contracts import (
    ImageEvidence,
    Level1Category,
    Level2Category,
    ProductSourceType,
    StableId,
)

UnevaluatedClaims = tuple[
    Literal["authenticity"],
    Literal["origin"],
    Literal["quality"],
    Literal["warehouse_acceptance"],
]


class ProductMaterialClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classification_id: UUID
    review_id: UUID
    input_snapshot_version: Annotated[int, Field(ge=1)]
    classification_version: Annotated[int, Field(ge=1)]
    confirmed_level_1: Level1Category
    confirmed_level_2: Level2Category
    confirmed_by: StableId
    confirmed_at: datetime


class ProductMaterialManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    album_product_id: StableId
    snapshot_version: Annotated[int, Field(ge=1)]
    source_type: ProductSourceType
    source_event_id: UUID
    source_import_task_id: StableId
    images: Annotated[tuple[ImageEvidence, ...], Field(min_length=2, max_length=4)]
    classification: ProductMaterialClassification
    confirmation_event_id: UUID
    confirmation_schema_version: Literal["album-product-classification-confirmed/1.0"]
    claims_not_evaluated: UnevaluatedClaims
    mall_product_id: None
    bead_id: None
    publication_mode: Literal["draft_only"]
    auto_publish: Literal[False]


class ProductMaterialManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["opc-product-material-manifest/1.0"]
    opc_id: StableId
    items: list[ProductMaterialManifestItem]
    next_before: str | None
