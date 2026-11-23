from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import RFC_4122, UUID

from pydantic import Field, field_validator

from photo_organizer.categorization.contracts import FrozenModel, StableId
from photo_organizer.schemas.controlled_upload_candidate import (
    ControlledUploadCandidate,
)
from photo_organizer.schemas.real_sku_pilot import PilotClaims


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


def _canonical_user(value: str) -> str:
    prefix = "user:"
    if not value.startswith(prefix):
        raise ValueError("user identity must use the user namespace")
    parsed = UUID(value.removeprefix(prefix))
    if (
        f"{prefix}{parsed}" != value
        or parsed.version not in {1, 2, 3, 4, 5}
        or parsed.variant != RFC_4122
    ):
        raise ValueError("user identity must be canonical")
    return value


class ControlledUploadBindingRequest(FrozenModel):
    schema_version: Literal["opc-controlled-upload-binding-request/1.0"]
    task_id: UUID
    candidate: ControlledUploadCandidate

    @field_validator("task_id", mode="before")
    @classmethod
    def task_id_must_be_canonical(cls, value: object) -> object:
        return _canonical_uuid(value)


class UploadBindingUnauthorizedError(FrozenModel):
    detail: Literal["invalid_bearer_token"]


class UploadBindingNotFoundError(FrozenModel):
    detail: Literal["opc_not_found", "task_not_found"]


class UploadBindingConflictError(FrozenModel):
    detail: Literal[
        "candidate_opc_mismatch",
        "task_not_open",
        "candidate_already_bound",
    ]


class UploadBindingInvalidCandidateError(FrozenModel):
    detail: Literal["invalid_candidate"]


class ControlledUploadBindingResponse(FrozenModel):
    schema_version: Literal["opc-controlled-upload-binding/1.0"]
    binding_id: UUID
    candidate_id: Annotated[
        str,
        Field(pattern=r"^opc-controlled-upload-candidate:[0-9a-f]{64}$"),
    ]
    source_readiness_id: Annotated[
        str,
        Field(pattern=r"^opc-real-sku-pilot-readiness:[0-9a-f]{64}$"),
    ]
    opc_id: StableId
    task_id: UUID
    album_product_id: StableId
    source_type: Literal["first_sale_bead"]
    item_count: Annotated[int, Field(ge=2, le=4)]
    bound_by: StableId
    bound_at: datetime
    opc_authority_state: Literal[
        "verified_by_authenticated_membership_and_task_binding"
    ]
    task_binding_state: Literal["bound"]
    upload_state: Literal["ready_for_queue_handoff"]
    next_action: Literal["enqueue_candidate_items"]
    claims_not_evaluated: PilotClaims
    mall_product_id: None
    bead_id: None
    publication_state: Literal["not_created"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]

    @field_validator("binding_id", "task_id", mode="before")
    @classmethod
    def ids_must_be_canonical(cls, value: object) -> object:
        return _canonical_uuid(value)

    @field_validator("bound_by")
    @classmethod
    def bound_by_must_be_canonical(cls, value: str) -> str:
        return _canonical_user(value)

    @field_validator("bound_at")
    @classmethod
    def bound_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value)


class UploadCandidateBoundEvent(FrozenModel):
    schema_version: Literal["opc-controlled-upload-binding-event/1.0"]
    event_type: Literal["upload.candidate.bound"]
    event_version: Literal[1]
    binding_id: UUID
    candidate_id: Annotated[
        str,
        Field(pattern=r"^opc-controlled-upload-candidate:[0-9a-f]{64}$"),
    ]
    source_readiness_id: Annotated[
        str,
        Field(pattern=r"^opc-real-sku-pilot-readiness:[0-9a-f]{64}$"),
    ]
    opc_id: StableId
    task_id: UUID
    album_product_id: StableId
    source_type: Literal["first_sale_bead"]
    item_count: Annotated[int, Field(ge=2, le=4)]
    bound_by: StableId
    occurred_at: datetime
    opc_authority_state: Literal[
        "verified_by_authenticated_membership_and_task_binding"
    ]
    task_binding_state: Literal["bound"]
    upload_state: Literal["ready_for_queue_handoff"]
    next_action: Literal["enqueue_candidate_items"]
    claims_not_evaluated: PilotClaims
    mall_product_id: None
    bead_id: None
    publication_state: Literal["not_created"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]

    @field_validator("binding_id", "task_id", mode="before")
    @classmethod
    def ids_must_be_canonical(cls, value: object) -> object:
        return _canonical_uuid(value)

    @field_validator("bound_by")
    @classmethod
    def bound_by_must_be_canonical(cls, value: str) -> str:
        return _canonical_user(value)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_utc(cls, value: datetime) -> datetime:
        return _utc(value)
