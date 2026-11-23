from datetime import datetime
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
from photo_organizer.schemas.classification_reviews import ClassificationReviewResponse
from photo_organizer.schemas.group_confirmation import (
    AlbumProductGroupConfirmationResponse,
)
from photo_organizer.schemas.product_material_download import (
    ProductMaterialDownloadResponse,
)
from photo_organizer.schemas.product_material_manifest import (
    ProductMaterialManifestResponse,
)

MaterialDataClassification = Literal[
    "synthetic_no_customer_data",
    "opc_owned_product_material_no_customer_data",
]
UnevaluatedClaims = tuple[
    Literal["authenticity"],
    Literal["origin"],
    Literal["quality"],
    Literal["warehouse_acceptance"],
]


class SkuMaterialAcceptanceInputs(FrozenModel):
    group: AlbumProductGroupConfirmationResponse
    review: ClassificationReviewResponse
    manifest: ProductMaterialManifestResponse
    downloads: Annotated[
        tuple[ProductMaterialDownloadResponse, ...],
        Field(min_length=2, max_length=4),
    ]


class AcceptedImageEvidence(FrozenModel):
    asset_id: UUID
    evidence_asset_id: UUID
    role: Literal["original"]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    byte_size: Annotated[int, Field(gt=0, le=104_857_600)]
    media_type: Literal["image/jpeg", "image/png", "image/heic", "image/heif"]
    captured_at: datetime | None
    imported_at: datetime
    storage_ref: Annotated[
        str,
        Field(
            min_length=54,
            max_length=54,
            pattern=(
                r"^evidence/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/original$"
            ),
        ),
    ]


class _SkuMaterialAcceptanceReceiptBase(FrozenModel):
    acceptance_id: StableId
    validated_at: datetime
    data_classification: MaterialDataClassification
    acceptance_status: Literal["material_ready_for_mall_draft_not_listed"]
    opc_id: StableId
    album_product_id: StableId
    snapshot_version: Annotated[int, Field(ge=1)]
    source_type: ProductSourceType
    source_event_id: UUID
    classification_id: UUID
    review_id: UUID
    classification_version: Annotated[int, Field(ge=1)]
    confirmed_level_1: Level1Category
    confirmed_level_2: Level2Category
    confirmed_by: StableId
    confirmed_at: datetime
    images: Annotated[
        tuple[AcceptedImageEvidence, ...], Field(min_length=2, max_length=4)
    ]
    download_verified_asset_ids: Annotated[
        tuple[UUID, ...],
        Field(min_length=2, max_length=4),
    ]
    download_ttl_seconds: Literal[300]
    claims_not_evaluated: UnevaluatedClaims
    mall_product_id: None
    bead_id: None
    publication_mode: Literal["draft_only"]
    auto_publish: Literal[False]

    @field_validator("validated_at", "confirmed_at")
    @classmethod
    def timestamps_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("acceptance timestamps must include a timezone")
        return value


class SkuMaterialAcceptanceReceiptV1(_SkuMaterialAcceptanceReceiptBase):
    schema_version: Literal["opc-sku-material-acceptance/1.0"]


class SkuMaterialAcceptanceReceipt(_SkuMaterialAcceptanceReceiptBase):
    schema_version: Literal["opc-sku-material-acceptance/2.0"]
    download_confirmation_event_id: UUID
