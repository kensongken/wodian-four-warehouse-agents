from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import RFC_4122, UUID

from pydantic import Field, field_validator, model_validator

from photo_organizer.categorization.contracts import (
    FrozenModel,
    ProductSourceType,
    StableId,
)
from photo_organizer.schemas.real_sku_pilot import PilotClaims, PilotImageRole, Sha256

CandidateMediaType = Literal["image/jpeg", "image/png", "image/heic"]
_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
}


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


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value


def controlled_upload_candidate_id(payload: dict[str, object]) -> str:
    canonical_payload = dict(payload)
    canonical_payload.pop("candidate_id", None)
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"opc-controlled-upload-candidate:{sha256(canonical).hexdigest()}"


class ControlledUploadCandidateItem(FrozenModel):
    local_asset_id: UUID
    role: PilotImageRole
    position: Annotated[int, Field(ge=1, le=4)]
    filename: Annotated[
        str,
        Field(pattern=r"^[0-9a-f-]{36}\.(jpg|png|heic)$"),
    ]
    media_type: CandidateMediaType
    byte_size: Annotated[int, Field(gt=0, le=104_857_600)]
    sha256: Sha256
    captured_at: datetime
    human_privacy_reviewed: Literal[True]
    privacy_metadata_status: Literal["no_sensitive_metadata_detected"]
    retry: Literal[False]

    @field_validator("local_asset_id", mode="before")
    @classmethod
    def asset_id_must_be_canonical(cls, value: object) -> object:
        return _canonical_uuid(value)

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def filename_must_match_asset_and_media(self) -> Self:
        expected = f"{self.local_asset_id}.{_EXTENSIONS[self.media_type]}"
        if self.filename != expected:
            raise ValueError("filename must be derived from asset identity")
        return self


class ControlledUploadCandidate(FrozenModel):
    schema_version: Literal["opc-controlled-upload-candidate/1.0"]
    candidate_id: Annotated[
        str,
        Field(pattern=r"^opc-controlled-upload-candidate:[0-9a-f]{64}$"),
    ]
    status: Literal["ready_for_authenticated_task_binding"]
    source_readiness_id: Annotated[
        str,
        Field(pattern=r"^opc-real-sku-pilot-readiness:[0-9a-f]{64}$"),
    ]
    opc_id: StableId
    opc_authority_state: Literal["requires_authenticated_revalidation"]
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
    items: Annotated[
        tuple[ControlledUploadCandidateItem, ...],
        Field(min_length=2, max_length=4),
    ]
    claims_not_evaluated: PilotClaims
    target_task_id: None
    task_binding_state: Literal["not_bound"]
    upload_state: Literal["not_started"]
    next_action: Literal["bind_to_verified_opc_upload_task"]
    mall_product_id: None
    bead_id: None
    publication_state: Literal["not_created"]
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
    def items_and_identity_must_be_bound(self) -> Self:
        if tuple(item.position for item in self.items) != tuple(
            range(1, len(self.items) + 1)
        ):
            raise ValueError("item positions must be continuous")
        if self.items[0].role != "primary" or any(
            item.role != "detail" for item in self.items[1:]
        ):
            raise ValueError("item roles must be primary then detail")
        if len({item.local_asset_id for item in self.items}) != len(self.items):
            raise ValueError("item identities must be unique")
        payload = self.model_dump(mode="json")
        if self.candidate_id != controlled_upload_candidate_id(payload):
            raise ValueError("candidate identity must match content")
        return self
