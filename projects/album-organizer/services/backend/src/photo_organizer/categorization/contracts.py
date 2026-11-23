from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from photo_organizer.domain.limits import MAX_TASK_ACCEPTED_ASSETS


class ProductSourceType(StrEnum):
    FIRST_SALE_BEAD = "first_sale_bead"
    LEGACY_MATERIAL = "legacy_material"
    REDESIGN_OUTPUT = "redesign_output"


class Level1Category(StrEnum):
    NAN_HONG = "南红"
    AGARWOOD = "沉香"
    JADEITE = "翡翠"
    HETIAN_JADE = "和田玉"
    AMBER = "蜜蜡"
    PEARL = "珍珠"
    CRYSTAL = "水晶"
    COLORED_GEM = "彩宝"
    PRECIOUS_METAL = "贵金属"
    OTHER = "其他"
    UNKNOWN = "无法判断"


class Level2Category(StrEnum):
    BRACELET = "手串"
    NECKLACE = "项链"
    PENDANT = "吊坠"
    HANGING_ORNAMENT = "挂件"
    RING = "戒指"
    EARRING = "耳饰"
    BANGLE = "手镯"
    LOOSE_BEAD = "珠子/散珠"
    ORNAMENT = "摆件"
    ACCESSORY = "配件"
    OTHER = "其他"
    UNKNOWN = "无法判断"


class ReasonCode(StrEnum):
    SEED_MATCH = "seed_match"
    VISUAL_PATTERN = "visual_pattern"
    SHAPE_PATTERN = "shape_pattern"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


StableId = Annotated[
    str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$")
]
Confidence = Annotated[float, Field(ge=0, le=1)]
NormalizedCoordinate = Annotated[float, Field(ge=0, le=1)]


class ImageTransform(FrozenModel):
    kind: Literal["crop"]
    crop_box: tuple[
        NormalizedCoordinate,
        NormalizedCoordinate,
        NormalizedCoordinate,
        NormalizedCoordinate,
    ]
    tool_version: Annotated[
        str,
        Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$"),
    ]


class ImageEvidence(FrozenModel):
    asset_id: UUID
    role: Literal["original", "crop"]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    byte_size: Annotated[int, Field(gt=0, le=104_857_600)]
    media_type: Literal["image/jpeg", "image/png", "image/heic", "image/heif"]
    captured_at: datetime | None
    imported_at: datetime
    storage_ref: Annotated[str, Field(min_length=12, max_length=512)]
    source_asset_id: UUID | None
    transform: ImageTransform | None

    @field_validator("captured_at", "imported_at")
    @classmethod
    def timestamps_must_have_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("image timestamps must include a timezone")
        return value

    @field_validator("storage_ref")
    @classmethod
    def storage_ref_must_be_private_and_controlled(cls, value: str) -> str:
        if not value.startswith("evidence/"):
            raise ValueError("storage_ref must be a controlled evidence reference")
        parts = value.split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise ValueError("storage_ref must not contain unsafe path segments")
        if any(
            not all(character.isalnum() or character in "._-" for character in part)
            for part in parts
        ):
            raise ValueError("storage_ref contains unsupported characters")
        return value

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if self.role == "crop":
            if self.source_asset_id is None or self.transform is None:
                raise ValueError("a crop requires source_asset_id and transform")
        elif self.source_asset_id is not None or self.transform is not None:
            raise ValueError("an original cannot carry crop provenance")
        return self


class HumanConfirmation(FrozenModel):
    status: Literal["confirmed"]
    confirmed_by: StableId
    confirmed_at: datetime
    confirmation_scope: Literal["grouping_membership"]

    @field_validator("confirmed_at")
    @classmethod
    def confirmed_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("confirmed_at must include a timezone")
        return value


class CategorizationSeed(FrozenModel):
    level_1_candidate: Level1Category
    level_2_candidate: Level2Category
    candidate_source: Literal["operator_seed", "ai_candidate"]
    confidence: Confidence | None
    assertion_scope: Literal["candidate_only"]


class Consent(FrozenModel):
    status: Literal["not_required"]
    basis: Literal["opc_owned_product_material"]


AllowedConsumer = Literal["categorization", "mall_draft_export"]


class ConfirmedSnapshot(FrozenModel):
    schema_version: Literal["album-product-categorization/1.0"]
    event_id: UUID
    event_type: Literal["album.product.snapshot.confirmed.v1"]
    occurred_at: datetime
    opc_id: StableId
    album_product_id: StableId
    snapshot_version: Annotated[int, Field(ge=1)]
    source_import_task_id: StableId
    source_type: ProductSourceType
    images: Annotated[tuple[ImageEvidence, ...], Field(min_length=2, max_length=4)]
    human_confirmation: HumanConfirmation
    categorization_input: CategorizationSeed
    consent: Consent
    allowed_consumers: Annotated[
        tuple[AllowedConsumer, ...], Field(min_length=1, max_length=2)
    ]
    mall_product_id: None
    bead_id: None

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_group_evidence(self) -> Self:
        originals = {
            image.asset_id for image in self.images if image.role == "original"
        }
        if not originals:
            raise ValueError("a confirmed snapshot requires an original")
        asset_ids = [image.asset_id for image in self.images]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("asset_id must be unique within a snapshot")
        if any(
            image.role == "crop" and image.source_asset_id not in originals
            for image in self.images
        ):
            raise ValueError("a crop must reference an original in the same snapshot")
        if "categorization" not in self.allowed_consumers:
            raise ValueError("categorization consumer permission is required")
        if len(set(self.allowed_consumers)) != len(self.allowed_consumers):
            raise ValueError("allowed_consumers must be unique")
        return self


class ConfirmedSnapshotBatch(FrozenModel):
    schema_version: Literal["album-product-categorization/1.0"]
    batch_id: UUID
    generated_at: datetime
    data_classification: Literal[
        "synthetic_no_customer_data",
        "opc_owned_product_material_no_customer_data",
    ]
    events: Annotated[
        tuple[ConfirmedSnapshot, ...],
        Field(min_length=1, max_length=MAX_TASK_ACCEPTED_ASSETS),
    ]

    @field_validator("generated_at")
    @classmethod
    def generated_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("generated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def event_identities_must_be_unique(self) -> Self:
        event_ids = [event.event_id for event in self.events]
        product_versions = [
            (event.album_product_id, event.snapshot_version) for event in self.events
        ]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("event_id must be unique within a batch")
        if len(set(product_versions)) != len(product_versions):
            raise ValueError("album product snapshot must be unique within a batch")
        return self


class CategorizationRequest(FrozenModel):
    input_event_id: UUID
    opc_id: StableId
    album_product_id: StableId
    input_snapshot_version: Annotated[int, Field(ge=1)]
    source_type: ProductSourceType
    images: Annotated[tuple[ImageEvidence, ...], Field(min_length=2, max_length=4)]
    seed: CategorizationSeed


class EngineCandidate(FrozenModel):
    level_1: Level1Category
    level_2: Level2Category
    confidence: Confidence
    reason_codes: Annotated[tuple[ReasonCode, ...], Field(min_length=1, max_length=4)]

    @field_validator("reason_codes")
    @classmethod
    def reason_codes_must_be_unique(
        cls, value: tuple[ReasonCode, ...]
    ) -> tuple[ReasonCode, ...]:
        if len(set(value)) != len(value):
            raise ValueError("reason_codes must be unique")
        return value


class EngineIdentity(FrozenModel):
    name: StableId
    version: StableId


class RankedCandidate(FrozenModel):
    rank: Annotated[int, Field(ge=1, le=3)]
    level_1: Level1Category
    level_2: Level2Category
    confidence: Confidence
    reason_codes: Annotated[tuple[ReasonCode, ...], Field(min_length=1, max_length=4)]


class ClassificationCandidateEvent(FrozenModel):
    schema_version: Literal["album-product-classification/1.0"]
    event_id: UUID
    event_type: Literal["album.product.classification.candidate.v1"]
    occurred_at: datetime
    input_event_id: UUID
    opc_id: StableId
    album_product_id: StableId
    input_snapshot_version: Annotated[int, Field(ge=1)]
    source_type: ProductSourceType
    classification_version: Literal[1]
    engine: EngineIdentity
    candidates: Annotated[
        tuple[RankedCandidate, ...], Field(min_length=1, max_length=3)
    ]
    review_status: Literal["requires_review"]
    claims_not_evaluated: tuple[
        Literal["authenticity"],
        Literal["origin"],
        Literal["quality"],
        Literal["warehouse_acceptance"],
    ]
    mall_product_id: None
    bead_id: None


class CategorizationFailure(FrozenModel):
    input_event_id: UUID
    album_product_id: StableId
    code: Literal["categorization_unavailable", "invalid_engine_result"]
    retryable: bool


class CategorizationBatchResult(FrozenModel):
    schema_version: Literal["album-product-classification-batch/1.0"]
    input_batch_id: UUID
    generated_at: datetime
    events: tuple[ClassificationCandidateEvent, ...]
    failures: tuple[CategorizationFailure, ...]


class ConfirmedClassificationEvent(FrozenModel):
    schema_version: Literal["album-product-classification-confirmed/1.0"]
    event_id: UUID
    event_type: Literal["album.product.classification.confirmed.v1"]
    occurred_at: datetime
    opc_id: StableId
    album_product_id: StableId
    source_type: ProductSourceType
    classification_id: UUID
    review_id: UUID
    input_event_id: UUID
    input_snapshot_version: Annotated[int, Field(ge=1)]
    classification_version: Annotated[int, Field(ge=1)]
    confirmed_level_1: Level1Category
    confirmed_level_2: Level2Category
    confirmed_by: StableId
    confirmed_at: datetime
    claims_not_evaluated: tuple[
        Literal["authenticity"],
        Literal["origin"],
        Literal["quality"],
        Literal["warehouse_acceptance"],
    ]
    mall_product_id: None
    bead_id: None

    @field_validator("occurred_at", "confirmed_at")
    @classmethod
    def confirmation_timestamps_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("confirmation timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def confirmation_time_is_event_time(self) -> Self:
        if self.occurred_at != self.confirmed_at:
            raise ValueError("confirmed_at must equal occurred_at")
        return self
