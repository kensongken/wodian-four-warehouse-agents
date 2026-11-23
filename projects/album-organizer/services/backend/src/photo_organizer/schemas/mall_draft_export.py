from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from photo_organizer.categorization.contracts import (
    FrozenModel,
    Level1Category,
    Level2Category,
    ProductSourceType,
    StableId,
)
from photo_organizer.schemas.acceptance_receipt import (
    AcceptedImageEvidence,
    MaterialDataClassification,
    UnevaluatedClaims,
)

BusinessFieldRequirements = tuple[
    Literal["title"],
    Literal["selling_points"],
    Literal["price"],
    Literal["inventory"],
    Literal["sku"],
    Literal["customization"],
    Literal["origin_evidence"],
]


class MallDraftDownloadLookup(FrozenModel):
    confirmation_event_id: UUID
    asset_id: UUID


class MallDraftClassification(FrozenModel):
    classification_id: UUID
    review_id: UUID
    classification_version: Annotated[int, Field(ge=1)]
    confirmed_level_1: Level1Category
    confirmed_level_2: Level2Category
    confirmed_by: StableId
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def confirmation_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("mall draft confirmation timestamp must be UTC")
        return value


class MallProductDraftRequest(FrozenModel):
    draft_request_id: StableId
    source_acceptance_id: StableId
    source_group_confirmation_event_id: UUID
    source_album_product_id: StableId
    source_snapshot_version: Annotated[int, Field(ge=1)]
    source_type: ProductSourceType
    classification: MallDraftClassification
    images: Annotated[
        tuple[AcceptedImageEvidence, ...],
        Field(min_length=2, max_length=4),
    ]
    download_lookup: Annotated[
        tuple[MallDraftDownloadLookup, ...],
        Field(min_length=2, max_length=4),
    ]
    required_business_fields: BusinessFieldRequirements
    claims_not_evaluated: UnevaluatedClaims
    mall_product_id: None
    bead_id: None
    publication_state: Literal["draft"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]


class MallProductDraftBatch(FrozenModel):
    schema_version: Literal["opc-mall-product-draft-batch/1.0"]
    batch_id: StableId
    opc_id: StableId
    data_classification: MaterialDataClassification
    export_version: Literal[1]
    generated_at: datetime
    operation: Literal["create_draft_only"]
    existing_product_policy: Literal["emit_change_proposal_do_not_overwrite"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]
    items: Annotated[
        tuple[MallProductDraftRequest, ...],
        Field(min_length=1, max_length=10),
    ]

    @field_validator("generated_at")
    @classmethod
    def generation_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("mall draft generation timestamp must be UTC")
        return value
