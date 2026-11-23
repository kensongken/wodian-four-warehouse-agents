"""Independent network contract for classification-to-style eligibility."""

import hashlib
import json
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

Sha256Hex = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")
]
ProductCode = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255),
]


class StyleEligibilityImageRefV4(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: UUID
    sha256: Sha256Hex
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    role: Literal["main_candidate", "gallery", "source_original"]


class StyleEligibilityFactV4(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1, max_length=128)
    evidence: Literal["human_confirmed"]


class StyleMaterialFactV4(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1, max_length=128)
    claim_basis: Literal["document_supported", "merchant_declared"]
    evidence_record_id: UUID
    evidence_content_sha256: Sha256Hex


class StyleEligibilityInputV4(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["4-style-eligibility-input"] = (
        "4-style-eligibility-input"
    )
    tenant_id: UUID
    product_code: ProductCode
    eligibility_input_version: int = Field(ge=1)
    source_material_package_version: int = Field(ge=1)
    source_classification_id: UUID
    source_classification_version: int = Field(ge=1)
    source_classification_sha256: Sha256Hex
    source_image_set_sha256: Sha256Hex
    main_image: StyleEligibilityImageRefV4
    images: tuple[StyleEligibilityImageRefV4, ...] = Field(
        min_length=1, max_length=3000
    )
    material: StyleMaterialFactV4
    form: StyleEligibilityFactV4
    transform_authorization_id: UUID | None = None
    transform_authorization_status: Literal["none", "active", "expired", "revoked"]
    transform_authorization_scope: Literal["local_style_acceptance_only"] | None = None
    transform_authorization_image_set_sha256: Sha256Hex | None = None
    transform_authorization_expires_at: AwareDatetime | None = None
    transform_authorization_revoked_at: AwareDatetime | None = None
    status: Literal["eligibility_only"] = "eligibility_only"
    created_at: AwareDatetime
    content_sha256: Sha256Hex
    automatic_generation_allowed: Literal[False] = False
    paid_provider_allowed: Literal[False] = False
    automatic_publication_allowed: Literal[False] = False
    automatic_customer_send_allowed: Literal[False] = False

    @field_validator(
        "automatic_generation_allowed",
        "paid_provider_allowed",
        "automatic_publication_allowed",
        "automatic_customer_send_allowed",
        mode="before",
    )
    @classmethod
    def require_exact_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("automatic permissions must be false")
        return value

    @model_validator(mode="after")
    def require_consistent_references(self) -> "StyleEligibilityInputV4":
        if self.main_image not in self.images:
            raise ValueError("main image must belong to images")
        authorization = (
            self.transform_authorization_id,
            self.transform_authorization_scope,
            self.transform_authorization_image_set_sha256,
            self.transform_authorization_expires_at,
        )
        if self.transform_authorization_status == "none":
            if any(value is not None for value in authorization) or (
                self.transform_authorization_revoked_at is not None
            ):
                raise ValueError("authorization snapshot is inconsistent")
        elif any(value is None for value in authorization):
            raise ValueError("authorization snapshot is incomplete")
        if (self.transform_authorization_status == "revoked") != (
            self.transform_authorization_revoked_at is not None
        ):
            raise ValueError("authorization revocation snapshot is inconsistent")
        return self


class StyleWarehouseDeliveryV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["style-warehouse-delivery-v3"] = (
        "style-warehouse-delivery-v3"
    )
    handoff_id: UUID
    idempotency_key: IdempotencyKey
    payload_sha256: Sha256Hex
    payload: StyleEligibilityInputV4

    @model_validator(mode="after")
    def bind_delivery(self) -> "StyleWarehouseDeliveryV3":
        if self.payload_sha256 != self.payload.content_sha256:
            raise ValueError("payload digest must match")
        expected = (
            f"style-v4:{self.payload.tenant_id}:"
            f"{self.payload.source_classification_id}"
        )
        if self.idempotency_key != expected:
            raise ValueError("idempotency key must match payload source")
        return self


class StyleWarehouseDecisionReceiptV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["style-warehouse-decision-receipt-v3"] = (
        "style-warehouse-decision-receipt-v3"
    )
    decision_receipt_id: UUID
    handoff_id: UUID
    checked_payload_sha256: Sha256Hex
    decision: Literal["accepted", "rejected"]
    rejection_code: str | None = Field(default=None, max_length=64)
    generation_job_id: None = None
    decided_at: AwareDatetime
    disposition: Literal["accepted", "duplicate"]

    @model_validator(mode="after")
    def require_decision_fields(self) -> "StyleWarehouseDecisionReceiptV3":
        if (self.decision == "rejected") != (self.rejection_code is not None):
            raise ValueError("rejection code must match decision")
        return self


def canonical_style_eligibility_v4_sha256(value: StyleEligibilityInputV4) -> str:
    body = value.model_dump(
        mode="json",
        exclude={"content_sha256", "eligibility_input_version", "created_at"},
    )
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
