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

from product_engine.contracts.common import ProductCode, Sha256Hex

IdempotencyKey = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255),
]


class StyleEligibilityImageRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: UUID
    sha256: Sha256Hex
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    role: Literal["main_candidate", "gallery", "source_original"]


class StyleEligibilityFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1, max_length=128)
    confirmation_status: Literal["human_confirmed"] = "human_confirmed"
    evidence: Literal["human_confirmed", "information_insufficient"]


class StyleTransformAuthorizationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_request_id: UUID
    scope: Literal["local_style_acceptance_only"]
    expires_at: AwareDatetime


class StyleEligibilityInputV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["2-style-eligibility-input"] = "2-style-eligibility-input"
    tenant_id: UUID
    product_code: ProductCode
    eligibility_input_version: int = Field(ge=1)
    source_material_package_version: int = Field(ge=1)
    source_classification_id: UUID
    source_classification_version: int = Field(ge=1)
    source_classification_sha256: Sha256Hex
    source_image_set_sha256: Sha256Hex
    main_image: StyleEligibilityImageRef
    images: tuple[StyleEligibilityImageRef, ...] = Field(min_length=1, max_length=3000)
    material: StyleEligibilityFact
    form: StyleEligibilityFact
    transform_authorization_id: UUID | None = None
    transform_authorization_status: Literal["none", "active", "expired", "revoked"]
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
    def require_consistent_references(self) -> "StyleEligibilityInputV2":
        if self.main_image not in self.images:
            raise ValueError("main image must belong to images")
        authorization_fields = (
            self.transform_authorization_id,
            self.transform_authorization_expires_at,
        )
        if self.transform_authorization_status == "none":
            if any(value is not None for value in authorization_fields) or (
                self.transform_authorization_revoked_at is not None
            ):
                raise ValueError("authorization snapshot is inconsistent")
        elif any(value is None for value in authorization_fields):
            raise ValueError("authorization snapshot is inconsistent")
        if self.transform_authorization_status == "revoked":
            if self.transform_authorization_revoked_at is None:
                raise ValueError("authorization snapshot is inconsistent")
        elif self.transform_authorization_revoked_at is not None:
            raise ValueError("authorization snapshot is inconsistent")
        return self


class StyleWarehouseDeliveryV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["style-warehouse-delivery-v1"] = "style-warehouse-delivery-v1"
    handoff_id: UUID
    eligibility_input_id: UUID
    idempotency_key: IdempotencyKey
    payload_sha256: Sha256Hex
    payload: StyleEligibilityInputV2

    @model_validator(mode="after")
    def bind_delivery_to_payload(self) -> "StyleWarehouseDeliveryV1":
        if self.payload_sha256 != self.payload.content_sha256:
            raise ValueError("payload digest must match")
        expected_key = f"style:{self.payload.tenant_id}:{self.eligibility_input_id}"
        if self.idempotency_key != expected_key:
            raise ValueError("idempotency key must match eligibility input")
        return self


class StyleWarehouseDecisionReceiptV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["style-warehouse-decision-receipt-v1"] = (
        "style-warehouse-decision-receipt-v1"
    )
    decision_receipt_id: UUID
    handoff_id: UUID
    checked_payload_sha256: Sha256Hex
    decision: Literal["accepted", "rejected"]
    rejection_code: str | None = Field(default=None, max_length=64)
    generation_job_id: None = None
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def require_decision_fields(self) -> "StyleWarehouseDecisionReceiptV1":
        rejected = self.decision == "rejected"
        if rejected != (self.rejection_code is not None):
            raise ValueError("rejection code must match decision")
        return self


def canonical_style_eligibility_v2_sha256(value: StyleEligibilityInputV2) -> str:
    body = value.model_dump(
        mode="json",
        exclude={"content_sha256", "eligibility_input_version", "created_at"},
    )
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
