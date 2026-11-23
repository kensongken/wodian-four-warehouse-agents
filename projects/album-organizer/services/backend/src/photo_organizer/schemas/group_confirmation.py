from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from photo_organizer.categorization.contracts import (
    Level1Category,
    Level2Category,
    ProductSourceType,
)


class AlbumProductGroupConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmation_request_id: UUID
    evidence_asset_ids: Annotated[
        tuple[UUID, ...],
        Field(
            min_length=2,
            max_length=4,
            json_schema_extra={"uniqueItems": True},
        ),
    ]
    source_type: ProductSourceType
    level_1_candidate: Level1Category
    level_2_candidate: Level2Category

    @model_validator(mode="after")
    def evidence_assets_must_be_unique(self) -> Self:
        if len(set(self.evidence_asset_ids)) != len(self.evidence_asset_ids):
            raise ValueError("evidence_asset_ids must be unique")
        return self


class AlbumProductGroupConfirmationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["opc-album-product-group-confirmation/1.0"]
    confirmation_event_id: UUID
    album_product_id: Annotated[str, Field(min_length=3, max_length=128)]
    snapshot_version: Literal[1]
    opc_id: Annotated[
        str,
        Field(
            min_length=3,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ]
    evidence_asset_ids: Annotated[tuple[UUID, ...], Field(min_length=2, max_length=4)]
    source_type: ProductSourceType
    confirmed_by: Annotated[str, Field(min_length=3, max_length=128)]
    confirmed_at: datetime
    claims_not_evaluated: tuple[
        Literal["authenticity"],
        Literal["origin"],
        Literal["quality"],
        Literal["warehouse_acceptance"],
    ]
    mall_product_id: None
    bead_id: None
    publication_mode: Literal["draft_only"]
    auto_publish: Literal[False]


class ExistingProductNextSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmation_request_id: UUID
    expected_current_snapshot_version: Annotated[int, Field(ge=1)]
    evidence_asset_ids: Annotated[
        tuple[UUID, ...],
        Field(
            min_length=2,
            max_length=4,
            json_schema_extra={"uniqueItems": True},
        ),
    ]
    source_type: ProductSourceType
    level_1_candidate: Level1Category
    level_2_candidate: Level2Category

    @model_validator(mode="after")
    def evidence_assets_must_be_unique(self) -> Self:
        if len(set(self.evidence_asset_ids)) != len(self.evidence_asset_ids):
            raise ValueError("evidence_asset_ids must be unique")
        return self


class ExistingProductNextSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["opc-existing-product-next-snapshot/1.0"]
    confirmation_event_id: UUID
    album_product_id: Annotated[str, Field(min_length=3, max_length=128)]
    snapshot_version: Annotated[int, Field(ge=2)]
    opc_id: Annotated[
        str,
        Field(
            min_length=3,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ]
    evidence_asset_ids: Annotated[tuple[UUID, ...], Field(min_length=2, max_length=4)]
    source_type: ProductSourceType
    confirmed_by: Annotated[str, Field(min_length=3, max_length=128)]
    confirmed_at: datetime
    claims_not_evaluated: tuple[
        Literal["authenticity"],
        Literal["origin"],
        Literal["quality"],
        Literal["warehouse_acceptance"],
    ]
    mall_product_id: None
    bead_id: None
    publication_mode: Literal["draft_only"]
    auto_publish: Literal[False]
