from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol, Self
from uuid import RFC_4122, UUID

from pydantic import Field, field_validator, model_validator

from photo_organizer.categorization.contracts import (
    FrozenModel,
    ProductSourceType,
    StableId,
)

PilotClaims = tuple[
    Literal["authenticity"],
    Literal["origin"],
    Literal["quality"],
    Literal["warehouse_acceptance"],
]
PilotImageRole = Literal["primary", "detail"]
PilotMediaType = Literal["image/jpeg", "image/png", "image/heic", "image/heif"]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _OrderedImage(Protocol):
    local_image_id: UUID
    role: PilotImageRole
    position: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value


def _canonical_uuid(value: object) -> object:
    if isinstance(value, str):
        parsed = UUID(value)
        if (
            str(parsed) != value
            or parsed.version not in {1, 2, 3, 4, 5}
            or parsed.variant != RFC_4122
        ):
            raise ValueError("UUID must be canonical RFC 4122")
    return value


def _validate_image_order(images: tuple[_OrderedImage, ...]) -> None:
    if tuple(image.position for image in images) != tuple(range(1, len(images) + 1)):
        raise ValueError("image positions must be continuous")
    if len({image.local_image_id for image in images}) != len(images):
        raise ValueError("image identities must be unique")
    if images[0].role != "primary" or any(
        image.role != "detail" for image in images[1:]
    ):
        raise ValueError("image roles must be primary then detail")


class PilotImageIntent(FrozenModel):
    local_image_id: UUID
    role: PilotImageRole
    position: Annotated[int, Field(ge=1, le=4)]
    captured_at: datetime
    human_privacy_reviewed: Literal[True]

    @field_validator("local_image_id", mode="before")
    @classmethod
    def image_id_must_be_canonical(cls, value: object) -> object:
        return _canonical_uuid(value)

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value)


class RealSkuPilotIntakeManifest(FrozenModel):
    schema_version: Literal["opc-real-sku-pilot-intake/1.0"]
    opc_id: StableId
    album_product_id: StableId
    source_type: ProductSourceType
    target_chain: Literal["sellable_product_material"]
    data_classification: Literal["opc_owned_product_material_no_customer_data"]
    material_owner: Literal["opc"]
    authorization_basis: Literal["opc_self_owned_product_material"]
    authorized_by: StableId
    authorized_at: datetime
    privacy_review_confirmed: Literal[True]
    customer_data_present: Literal[False]
    images: Annotated[tuple[PilotImageIntent, ...], Field(min_length=2, max_length=4)]
    claims_not_evaluated: PilotClaims
    mall_product_id: None
    bead_id: None
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]

    @field_validator("authorized_by")
    @classmethod
    def authorized_by_must_be_canonical_user(cls, value: str) -> str:
        prefix = "user:"
        if not value.startswith(prefix):
            raise ValueError("authorized_by must be a user identity")
        parsed = UUID(value.removeprefix(prefix))
        if (
            f"{prefix}{parsed}" != value
            or parsed.version not in {1, 2, 3, 4, 5}
            or parsed.variant != RFC_4122
        ):
            raise ValueError("authorized_by must be canonical")
        return value

    @field_validator("authorized_at")
    @classmethod
    def authorized_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def image_order_must_be_safe(self) -> Self:
        _validate_image_order(self.images)
        return self


class ValidatedPilotImage(FrozenModel):
    local_image_id: UUID
    role: PilotImageRole
    position: Annotated[int, Field(ge=1, le=4)]
    captured_at: datetime
    human_privacy_reviewed: Literal[True]
    sha256: Sha256
    byte_size: Annotated[int, Field(gt=0, le=104_857_600)]
    media_type: PilotMediaType
    privacy_metadata_status: Literal["no_sensitive_metadata_detected"]

    @field_validator("local_image_id", mode="before")
    @classmethod
    def image_id_must_be_canonical(cls, value: object) -> object:
        return _canonical_uuid(value)

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value)


class RealSkuPilotReadinessReceipt(FrozenModel):
    schema_version: Literal["opc-real-sku-pilot-readiness/1.0"]
    readiness_id: Annotated[
        str,
        Field(pattern=r"^opc-real-sku-pilot-readiness:[0-9a-f]{64}$"),
    ]
    status: Literal["eligible_for_controlled_import"]
    opc_id: StableId
    album_product_id: StableId
    source_type: Literal[ProductSourceType.FIRST_SALE_BEAD]
    target_chain: Literal["sellable_product_material"]
    data_classification: Literal["opc_owned_product_material_no_customer_data"]
    material_owner: Literal["opc"]
    authorization_basis: Literal["opc_self_owned_product_material"]
    authorized_by: StableId
    authorized_at: datetime
    privacy_review_confirmed: Literal[True]
    customer_data_present: Literal[False]
    images: Annotated[
        tuple[ValidatedPilotImage, ...], Field(min_length=2, max_length=4)
    ]
    claims_not_evaluated: PilotClaims
    next_action: Literal["upload_to_controlled_ingestion"]
    mall_product_id: None
    bead_id: None
    publication_state: Literal["not_created"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]

    @field_validator("authorized_at")
    @classmethod
    def authorized_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def image_order_must_be_safe(self) -> Self:
        _validate_image_order(self.images)
        return self
