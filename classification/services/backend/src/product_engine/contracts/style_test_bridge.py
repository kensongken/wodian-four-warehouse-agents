"""Strict metadata-only contract for the local classification style-test bridge."""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

PhotoCategory = Literal[
    "real_photo",
    "wear_photo",
    "scene_photo",
    "multi_product_photo",
]
ProductCategory = Literal[
    "agarwood",
    "turquoise",
    "nanhong",
    "hetian_jade",
    "colored_gemstone",
    "jadeite",
]
ProductForm = Literal[
    "bangle",
    "bead",
    "bracelet",
    "brooch",
    "earring",
    "necklace",
    "pendant",
    "ring",
    "strand",
    "round_bead_bracelet",
    "BraceletModel2026",
]
PRODUCT_FORM_DISPLAY_LABELS_ZH: dict[ProductForm, str] = {
    "bangle": "手镯",
    "bead": "单珠",
    "bracelet": "手串",
    "brooch": "胸针",
    "earring": "耳饰",
    "necklace": "项链",
    "pendant": "吊坠",
    "ring": "戒指",
    "strand": "串珠",
    "round_bead_bracelet": "方圆珠手串",
    "BraceletModel2026": "手串型号2026",
}
Sha256Hex = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
PositiveImageDimension = Annotated[int, Field(strict=True, gt=0)]
StrictConfirmationVersion = Annotated[int, Field(strict=True)]
CustomerTestId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^test-customer-[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
ProductId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^test-product-[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
ClassificationVersion = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^classification-v[1-9][0-9]*$",
    ),
]
StyleTestIdempotencyKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=255,
        pattern=(r"^style-test:test-product-[a-z0-9]+(?:-[a-z0-9]+)*:v[1-9][0-9]*$"),
    ),
]


class ConfirmedPhotoV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    photo_id: UUID
    content_sha256: Sha256Hex
    mime_type: Literal["image/jpeg", "image/png", "image/heic"]
    width: PositiveImageDimension
    height: PositiveImageDimension
    automatic_category: PhotoCategory
    confidence: Literal["high", "medium", "low"]
    human_corrected: StrictBool
    confirmed_category: PhotoCategory
    media_receipt_id: UUID


class ClassificationStyleHandoffV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["classification-style-handoff-v1"]
    handoff_id: UUID
    customer_test_id: CustomerTestId
    product_id: ProductId
    product_category: ProductCategory
    product_form: ProductForm
    classification_version: ClassificationVersion
    confirmation_version: StrictConfirmationVersion
    main_photo_id: UUID
    transform_permission: Literal[True]
    style_generation_allowed: Literal[True]
    photos: tuple[ConfirmedPhotoV1, ...]
    idempotency_key: StyleTestIdempotencyKey
    desensitized_test_only: Literal[True]
    source: Literal["classification_local_page"]
    publishable: Literal[False]
    production_eligible: Literal[False]

    @field_validator(
        "transform_permission",
        "style_generation_allowed",
        "desensitized_test_only",
        "publishable",
        "production_eligible",
        mode="before",
    )
    @classmethod
    def require_exact_boolean_wire_types(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("fixed boolean fields require JSON boolean values")
        return value

    @model_validator(mode="after")
    def enforce_photo_membership(self) -> "ClassificationStyleHandoffV1":
        photo_ids = [photo.photo_id for photo in self.photos]
        if len(photo_ids) != len(set(photo_ids)):
            raise ValueError("duplicate photo_id is not allowed")
        if not any(photo.confirmed_category == "real_photo" for photo in self.photos):
            raise ValueError("at least one confirmed real_photo is required")
        if photo_ids.count(self.main_photo_id) != 1:
            raise ValueError("main_photo_id must identify exactly one photo")
        expected_idempotency_key = f"style-test:{self.product_id}:v{self.confirmation_version}"
        if self.idempotency_key != expected_idempotency_key:
            raise ValueError("idempotency_key must match product and confirmation version")
        return self


def canonical_handoff_bytes(handoff: ClassificationStyleHandoffV1) -> bytes:
    """Return the single canonical JSON representation used for bridge digests."""

    return json.dumps(
        handoff.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def product_form_display_label_zh(product_form: ProductForm) -> str:
    """Return the display-only Chinese label for an approved transport code."""

    return PRODUCT_FORM_DISPLAY_LABELS_ZH[product_form]
