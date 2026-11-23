from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

StableId = Annotated[
    str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$")
]
ProductCode = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Confidence = Annotated[float, Field(ge=0, le=1)]
CategoryValue = Annotated[str, Field(min_length=1, max_length=64)]
ObjectKey = Annotated[str, Field(min_length=1, max_length=1024)]


class HandoffAdapterError(ValueError):
    """Stable, non-sensitive reason that an outbound handoff is not safe."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceImage(_FrozenModel):
    asset_id: UUID
    sha256: Sha256
    mime_type: Literal["image/heic", "image/heif", "image/jpeg", "image/png"]
    kind: Literal["original", "crop"]


class ProductImageSetSource(_FrozenModel):
    opc_id: StableId
    album_product_id: StableId
    snapshot_version: Annotated[int, Field(ge=1)]
    source_import_task_id: Annotated[str, Field(min_length=1, max_length=128)]
    images: Annotated[tuple[SourceImage, ...], Field(min_length=1, max_length=3000)]

    @model_validator(mode="after")
    def asset_ids_must_be_unique(self) -> Self:
        asset_ids = [image.asset_id for image in self.images]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("source asset_id must be unique")
        return self


class ClassificationCandidateSource(_FrozenModel):
    level_1: CategoryValue
    level_2: CategoryValue
    confidence: Confidence


class ConfirmedClassificationSource(_FrozenModel):
    classification_id: UUID
    opc_id: StableId
    album_product_id: StableId
    input_snapshot_version: Annotated[int, Field(ge=1)]
    status: Literal["confirmed"]
    confirmed_level_1: CategoryValue
    confirmed_level_2: CategoryValue
    confirmed_at: datetime
    candidates: Annotated[
        tuple[ClassificationCandidateSource, ...], Field(min_length=1, max_length=3)
    ]

    @field_validator("confirmed_at")
    @classmethod
    def confirmed_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("confirmed_at must include a timezone")
        return value


class OpcExternalTenantBinding(_FrozenModel):
    binding_id: UUID
    binding_source: Literal["operator_provisioned"]
    opc_id: StableId
    tenant_id: UUID


class ProductCodeBinding(_FrozenModel):
    binding_id: UUID
    binding_source: Literal["operator_provisioned"]
    opc_id: StableId
    album_product_id: StableId
    product_code: ProductCode


class PreparedImageHandoff(_FrozenModel):
    asset_id: UUID
    object_key: ObjectKey
    role: Literal["main_candidate", "gallery", "source_original"]
    quality_confidence: Confidence
    storage_status: Literal["ready"]


class ProductImageRefV1(_FrozenModel):
    asset_id: UUID
    object_key: ObjectKey
    sha256: Sha256
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    kind: Literal["original", "crop"]
    role: Literal["main_candidate", "gallery", "source_original"]
    quality_confidence: Confidence


class ProductImageSetReadyV1(_FrozenModel):
    schema_version: Literal[1]
    event_id: UUID
    occurred_at: datetime
    tenant_id: UUID
    source_task_id: UUID
    product_code: ProductCode
    category: CategoryValue
    form: CategoryValue
    classification_confidence: Confidence
    images: Annotated[
        tuple[ProductImageRefV1, ...], Field(min_length=1, max_length=3000)
    ]

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def images_must_remain_inside_the_tenant(self) -> Self:
        tenant_prefix = f"tenants/{self.tenant_id}/"
        object_keys = [image.object_key for image in self.images]
        if any(not key.startswith(tenant_prefix) for key in object_keys):
            raise ValueError("object_key_tenant_mismatch")
        if len(set(object_keys)) != len(object_keys):
            raise ValueError("duplicate_object_key")
        return self


def build_product_image_set_ready(
    source: ProductImageSetSource,
    classification: ConfirmedClassificationSource,
    tenant_binding: OpcExternalTenantBinding,
    product_binding: ProductCodeBinding,
    prepared_images: Sequence[PreparedImageHandoff],
) -> ProductImageSetReadyV1:
    source = _validated_model(source)
    classification = _validated_model(classification)
    tenant_binding = _validated_model(tenant_binding)
    product_binding = _validated_model(product_binding)
    prepared_images = tuple(_validated_model(image) for image in prepared_images)
    if tenant_binding.binding_source != "operator_provisioned":
        raise HandoffAdapterError("tenant_binding_not_authoritative")
    if product_binding.binding_source != "operator_provisioned":
        raise HandoffAdapterError("product_binding_not_authoritative")
    if any(image.storage_status != "ready" for image in prepared_images):
        raise HandoffAdapterError("image_storage_not_ready")
    if tenant_binding.opc_id != source.opc_id:
        raise HandoffAdapterError("tenant_binding_mismatch")
    if (
        product_binding.opc_id != source.opc_id
        or product_binding.album_product_id != source.album_product_id
    ):
        raise HandoffAdapterError("product_binding_mismatch")
    if (
        classification.opc_id != source.opc_id
        or classification.album_product_id != source.album_product_id
        or classification.input_snapshot_version != source.snapshot_version
    ):
        raise HandoffAdapterError("classification_source_mismatch")
    try:
        source_task_id = UUID(source.source_import_task_id)
    except ValueError:
        raise HandoffAdapterError("invalid_source_task_id") from None

    matching_candidates = tuple(
        candidate
        for candidate in classification.candidates
        if candidate.level_1 == classification.confirmed_level_1
        and candidate.level_2 == classification.confirmed_level_2
    )
    if len(matching_candidates) != 1:
        raise HandoffAdapterError("classification_confidence_ambiguous")

    prepared_by_asset = {image.asset_id: image for image in prepared_images}
    source_asset_ids = {image.asset_id for image in source.images}
    if (
        len(prepared_by_asset) != len(prepared_images)
        or set(prepared_by_asset) != source_asset_ids
    ):
        raise HandoffAdapterError("prepared_image_set_mismatch")
    object_keys = [image.object_key for image in prepared_images]
    if len(set(object_keys)) != len(object_keys):
        raise HandoffAdapterError("duplicate_object_key")

    tenant_prefix = f"tenants/{tenant_binding.tenant_id}/"
    output_images: list[ProductImageRefV1] = []
    for image in source.images:
        if image.mime_type == "image/heif":
            raise HandoffAdapterError("unsupported_handoff_media")
        prepared = prepared_by_asset[image.asset_id]
        if not prepared.object_key.startswith(tenant_prefix):
            raise HandoffAdapterError("object_key_tenant_mismatch")
        output_images.append(
            ProductImageRefV1(
                asset_id=image.asset_id,
                object_key=prepared.object_key,
                sha256=image.sha256,
                mime_type=image.mime_type,
                kind=image.kind,
                role=prepared.role,
                quality_confidence=prepared.quality_confidence,
            )
        )

    event_id = product_image_set_event_id(classification.classification_id)
    return ProductImageSetReadyV1(
        schema_version=1,
        event_id=event_id,
        occurred_at=classification.confirmed_at,
        tenant_id=tenant_binding.tenant_id,
        source_task_id=source_task_id,
        product_code=product_binding.product_code,
        category=classification.confirmed_level_1,
        form=classification.confirmed_level_2,
        classification_confidence=matching_candidates[0].confidence,
        images=tuple(output_images),
    )


def product_image_set_event_id(classification_id: UUID) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"photo-organizer:product-image-set-ready:v1:{classification_id}",
    )


def _validated_model[ModelT: _FrozenModel](value: ModelT) -> ModelT:
    model_type = type(value)
    if set(value.__dict__) != set(model_type.model_fields):
        raise HandoffAdapterError("invalid_adapter_input")
    try:
        return model_type.model_validate(value.model_dump(mode="python"))
    except ValidationError:
        raise HandoffAdapterError("invalid_adapter_input") from None


def canonical_body(event: ProductImageSetReadyV1) -> bytes:
    return json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_body_sha256(event: ProductImageSetReadyV1) -> str:
    return sha256(canonical_body(event)).hexdigest()


def replay_disposition(
    existing_canonical_sha256: str | None,
    incoming_canonical_sha256: str,
) -> Literal["created", "replayed"]:
    if existing_canonical_sha256 is None:
        return "created"
    if existing_canonical_sha256 == incoming_canonical_sha256:
        return "replayed"
    raise HandoffAdapterError("event_identity_conflict")
