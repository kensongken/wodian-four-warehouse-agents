"""Versioned Mall SaaS to customer-style intake contract."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Sha256Hex = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
BoundedId = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
]


class MallStyleAssetRefV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_asset_id: UUID
    source_content_sha256: Sha256Hex
    asset_role: Literal["label_photo", "product_photo"]
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    style_product_asset_id: UUID | None = None
    authorized_purposes: tuple[
        Literal["product_metadata_extraction", "style_subject_transform"], ...
    ] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def bind_role_to_authority(self) -> MallStyleAssetRefV1:
        purposes = set(self.authorized_purposes)
        if len(purposes) != len(self.authorized_purposes):
            raise ValueError("authorized purposes must be unique")
        if self.asset_role == "label_photo":
            if self.style_product_asset_id is not None:
                raise ValueError("label photo cannot bind a product subject asset")
            if purposes != {"product_metadata_extraction"}:
                raise ValueError("label photo is metadata-only")
        elif (
            self.style_product_asset_id is None
            or "style_subject_transform" not in purposes
        ):
            raise ValueError("product photo requires an authorized style asset")
        return self


class MallStyleSelectionV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scene_style_id: UUID
    style_card_version_id: UUID
    slot_key: Annotated[
        str, StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    ]


class MallStyleAvailableSelectionV1(MallStyleSelectionV1):
    """One current customer-confirmed style/slot the Mall may select."""

    main_style_card_version_id: UUID | None = None
    scene_style_card_id: UUID
    scene_name: str
    style_version: int = Field(ge=1)
    slot_id: UUID
    slot_label: str
    slot_rules: dict[str, object]
    is_default: bool


class MallStyleSelectionListV1(BaseModel):
    """Read-only selection catalogue with an explicit ambiguity state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-style-selection-list-v1"] = (
        "mall-style-selection-list-v1"
    )
    merchant_id: UUID
    opc_id: UUID
    selection_status: Literal["ready", "selection_required", "unavailable"]
    selection_reason: Literal[
        "sole_selection",
        "explicit_default",
        "multiple_without_default",
        "multiple_defaults",
        "no_confirmed_style",
    ]
    default_selection: MallStyleSelectionV1 | None
    selections: tuple[MallStyleAvailableSelectionV1, ...]


class MallProductAssetImportReceiptV1(BaseModel):
    """Result of a byte-bearing import that starts no downstream workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-product-asset-import-receipt-v1"] = (
        "mall-product-asset-import-receipt-v1"
    )
    merchant_id: UUID
    opc_id: UUID
    source_asset_id: UUID
    source_content_sha256: Sha256Hex
    authorization_reference: BoundedId
    product_id: BoundedId
    style_product_asset_id: UUID
    mime_type: Literal["image/jpeg", "image/png"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    disposition: Literal["accepted", "duplicate"]
    subject_confirmation_required: Literal[True] = True
    generation_started: Literal[False] = False
    publishable: Literal[False] = False


class MallStyleGenerationApprovalV1(BaseModel):
    """One explicit, bounded fee approval that may enqueue exactly one request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-style-generation-approval-v1"] = (
        "mall-style-generation-approval-v1"
    )
    receipt_id: UUID
    product_id: BoundedId
    idempotency_key: BoundedId
    approval_reference: BoundedId
    provider: BoundedId
    model: BoundedId
    owner_actor_reference: BoundedId
    actor_role: Literal["merchant_owner"]
    automatic_retry_allowed: Literal[False] = False
    approved_candidate_count: int = Field(ge=1, le=3)
    approved_maximum_cost_usd: Decimal = Field(ge=0, decimal_places=6)
    currency: Literal["USD"] = "USD"
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    approval_payload_sha256: Sha256Hex
    automatic_publication_allowed: Literal[False] = False
    publishable: Literal[False] = False

    @field_validator(
        "automatic_retry_allowed",
        "automatic_publication_allowed",
        "publishable",
        mode="before",
    )
    @classmethod
    def require_no_publication(cls, value: object) -> object:
        if value is not False:
            raise ValueError("publication must remain false")
        return value

    @model_validator(mode="after")
    def bind_approval(self) -> MallStyleGenerationApprovalV1:
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval time")
        if self.idempotency_key != f"mall-style-generation:{self.receipt_id}":
            raise ValueError("idempotency key must bind the receipt")
        if self.approval_payload_sha256 != canonical_mall_generation_approval_sha256(
            self
        ):
            raise ValueError("approval digest must match canonical content")
        return self


class MallStyleGenerationQueueReceiptV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-style-generation-queue-receipt-v1"] = (
        "mall-style-generation-queue-receipt-v1"
    )
    generation_request_id: UUID
    receipt_id: UUID
    merchant_id: UUID
    opc_id: UUID
    product_id: BoundedId
    approval_reference: BoundedId
    rights_reference: BoundedId
    provider: BoundedId
    model: BoundedId
    owner_actor_reference: BoundedId
    actor_role: Literal["merchant_owner"]
    automatic_retry_allowed: Literal[False] = False
    approval_expires_at: AwareDatetime
    candidate_count: int = Field(ge=1, le=3)
    maximum_cost_usd: Decimal = Field(ge=0, decimal_places=6)
    status: Literal["pending_execution"]
    generation_job_id: None = None
    disposition: Literal["accepted", "duplicate"]
    publishable: Literal[False] = False
    created_at: AwareDatetime
    updated_at: AwareDatetime


class MallStyleResultCandidateV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: UUID
    asset_version: Literal[1] = 1
    content_sha256: Sha256Hex
    mime_type: Literal["image/jpeg", "image/png"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    generation_job_id: UUID
    sequence_number: int = Field(ge=1, le=3)
    adapter_version: str
    style_card_version_id: UUID
    scene_style_id: UUID
    slot_key: str
    rights_reference: BoundedId
    generation_approval_reference: BoundedId
    approval_state: Literal["pending_review", "approved", "rejected"]
    availability: Literal["available", "revoked", "expired"]
    content_url: str | None
    publishable: Literal[False] = False
    created_at: AwareDatetime


class MallStyleResultListV1(BaseModel):
    """Tenant-bound Mall projection of one receipt or generation job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-style-result-list-v1"] = "mall-style-result-list-v1"
    merchant_id: UUID
    opc_id: UUID
    product_id: BoundedId
    receipt_id: UUID
    generation_request_id: UUID | None = None
    generation_job_id: UUID | None = None
    status: Literal[
        "awaiting_product_photo",
        "awaiting_subject_confirmation",
        "awaiting_generation_approval",
        "pending_execution",
        "generation_in_progress",
        "awaiting_review",
        "completed",
        "failed",
        "revoked",
        "expired",
    ]
    status_reason: str
    product_asset_id: UUID | None = None
    subject_seal_id: UUID | None = None
    style_card_version_id: UUID
    scene_style_id: UUID
    slot_key: str
    rights_reference: BoundedId | None = None
    generation_approval_reference: BoundedId | None = None
    candidates: tuple[MallStyleResultCandidateV1, ...]
    publishable: Literal[False] = False
    updated_at: AwareDatetime


class MallSubjectConfirmationMetadataV1(BaseModel):
    """Signed authority metadata accompanying the raw reviewed mask PNG."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-subject-confirmation-metadata-v1"] = (
        "mall-subject-confirmation-metadata-v1"
    )
    receipt_id: UUID
    product_id: BoundedId
    product_asset_sha256: Sha256Hex
    base_subject_seal_id: UUID
    base_mask_sha256: Sha256Hex
    proposal_version: Literal[1] = 1
    reviewed_mask_sha256: Sha256Hex
    owner_actor_reference: BoundedId
    actor_role: Literal["merchant_owner"]
    idempotency_key: BoundedId
    mime_type: Literal["image/png"]
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_true(cls, value: object) -> object:
        if value is not True:
            raise ValueError("human confirmation must be true")
        return value

    @model_validator(mode="after")
    def bind_confirmation_key(self) -> MallSubjectConfirmationMetadataV1:
        if self.idempotency_key != f"mall-subject-confirmation:{self.receipt_id}":
            raise ValueError("idempotency key must bind the receipt")
        return self


class MallSubjectInspectionV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-subject-inspection-v1"] = "mall-subject-inspection-v1"
    merchant_id: UUID
    opc_id: UUID
    receipt_id: UUID
    product_id: BoundedId
    product_asset_id: UUID
    product_asset_sha256: Sha256Hex
    status: Literal["proposal_required", "needs_human_review", "confirmed"]
    proposal_method: Literal["automatic", "manual_full_frame"] | None = None
    manual_confirmation_required: bool = True
    quality_warning: str | None = None
    base_subject_seal_id: UUID | None = None
    proposal_version: Literal[1] | None = None
    base_mask_sha256: Sha256Hex | None = None
    normalized_subject_sha256: Sha256Hex | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    original_url: str
    rgba_url: str | None = None
    mask_url: str | None = None
    reviewed_subject_seal_id: UUID | None = None
    owner_actor_reference: BoundedId | None = None
    audit_event_id: UUID | None = None
    publishable: Literal[False] = False


class MallSubjectConfirmationReceiptV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-subject-confirmation-receipt-v1"] = (
        "mall-subject-confirmation-receipt-v1"
    )
    confirmation_id: UUID
    merchant_id: UUID
    opc_id: UUID
    receipt_id: UUID
    product_id: BoundedId
    product_asset_id: UUID
    product_asset_sha256: Sha256Hex
    base_subject_seal_id: UUID
    base_mask_sha256: Sha256Hex
    proposal_version: Literal[1] = 1
    reviewed_mask_sha256: Sha256Hex
    reviewed_subject_seal_id: UUID
    owner_actor_reference: BoundedId
    actor_role: Literal["merchant_owner"]
    audit_event_id: UUID
    disposition: Literal["accepted", "duplicate"]
    publishable: Literal[False] = False
    created_at: AwareDatetime


class MallStyleHandoffV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-style-handoff-v1"] = "mall-style-handoff-v1"
    handoff_id: UUID
    merchant_id: UUID
    opc_id: UUID
    product_entry_submission_id: UUID
    product_entry_draft_version: int = Field(ge=1)
    product_entry_draft_sha256: Sha256Hex
    product_id: BoundedId
    correlation_id: BoundedId
    idempotency_key: BoundedId
    payload_sha256: Sha256Hex
    authorized_purpose: Literal["style_candidate_generation_for_merchant_review"]
    assets: tuple[MallStyleAssetRefV1, ...] = Field(min_length=1, max_length=20)
    selected_product_photo_source_asset_id: UUID | None = None
    style_selection: MallStyleSelectionV1
    candidate_count: int = Field(ge=1, le=3)
    created_at: AwareDatetime
    automatic_generation_allowed: Literal[False] = False
    automatic_publication_allowed: Literal[False] = False
    publishable: Literal[False] = False

    @field_validator(
        "automatic_generation_allowed",
        "automatic_publication_allowed",
        "publishable",
        mode="before",
    )
    @classmethod
    def require_exact_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("automatic permissions and publication must remain false")
        return value

    @model_validator(mode="after")
    def bind_assets_and_digest(self) -> MallStyleHandoffV1:
        source_ids = [asset.source_asset_id for asset in self.assets]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source asset ids must be unique")
        if not any(asset.asset_role == "label_photo" for asset in self.assets):
            raise ValueError("at least one label photo is required")
        product_assets = [
            asset for asset in self.assets if asset.asset_role == "product_photo"
        ]
        if not product_assets:
            if self.selected_product_photo_source_asset_id is not None:
                raise ValueError("selected product photo is unavailable")
        elif self.selected_product_photo_source_asset_id not in {
            asset.source_asset_id for asset in product_assets
        }:
            raise ValueError("selected product photo must reference a product photo")
        expected_key = f"mall-style:{self.product_entry_submission_id}"
        if self.idempotency_key != expected_key:
            raise ValueError("idempotency key must bind the product-entry submission")
        if self.payload_sha256 != canonical_mall_payload_sha256(self):
            raise ValueError("payload digest must match canonical content")
        return self


class MallStyleHandoffReceiptV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["mall-style-handoff-receipt-v1"] = (
        "mall-style-handoff-receipt-v1"
    )
    receipt_id: UUID
    handoff_id: UUID
    merchant_id: UUID
    opc_id: UUID
    product_entry_submission_id: UUID
    product_entry_draft_version: int = Field(ge=1)
    product_entry_draft_sha256: Sha256Hex
    correlation_id: str
    checked_payload_sha256: Sha256Hex
    status: Literal[
        "awaiting_product_photo",
        "awaiting_subject_confirmation",
        "awaiting_generation_approval",
    ]
    product_asset_id: UUID | None = None
    subject_seal_id: UUID | None = None
    scene_style_id: UUID
    style_card_version_id: UUID
    slot_key: str
    candidate_count: int = Field(ge=1, le=3)
    maximum_cost_usd: Decimal = Field(ge=0, decimal_places=6)
    approval_required: Literal[True] = True
    generation_job_id: None = None
    publishable: Literal[False] = False
    disposition: Literal["accepted", "duplicate"]
    received_at: AwareDatetime
    updated_at: AwareDatetime


def canonical_mall_payload_sha256(value: MallStyleHandoffV1) -> str:
    body = value.model_dump(mode="json", exclude={"payload_sha256"})
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_mall_handoff_bytes(value: MallStyleHandoffV1) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_mall_generation_approval_sha256(
    value: MallStyleGenerationApprovalV1,
) -> str:
    body = value.model_dump(mode="json", exclude={"approval_payload_sha256"})
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_mall_product_asset_signature_bytes(
    *,
    content: bytes,
    content_sha256: str,
    mime_type: str,
    asset_role: str,
    authorized_purpose: str,
    authorization_reference: str,
) -> bytes:
    """Bind byte content and every authority-bearing header to one signature."""
    metadata = json.dumps(
        {
            "asset_role": asset_role,
            "authorization_reference": authorization_reference,
            "authorized_purpose": authorized_purpose,
            "content_sha256": content_sha256,
            "mime_type": mime_type,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return metadata + b"\n" + content


def canonical_mall_subject_confirmation_signature_bytes(
    metadata: MallSubjectConfirmationMetadataV1 | dict[str, object],
    reviewed_mask_png: bytes,
) -> bytes:
    body = (
        metadata.model_dump(mode="json")
        if isinstance(metadata, MallSubjectConfirmationMetadataV1)
        else metadata
    )
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return canonical + b"\n" + reviewed_mask_png
