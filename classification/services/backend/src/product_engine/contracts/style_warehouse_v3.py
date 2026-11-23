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
from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityFact,
    StyleEligibilityImageRef,
)

StyleClaimBasis = Literal["document_supported", "merchant_declared", "information_insufficient"]
IdempotencyKey = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]


class StyleMaterialFactV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1, max_length=128)
    claim_basis: StyleClaimBasis
    evidence_record_id: UUID | None = None
    evidence_content_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def require_consistent_provenance(self) -> "StyleMaterialFactV3":
        has_provenance = (
            self.evidence_record_id is not None and self.evidence_content_sha256 is not None
        )
        if self.claim_basis == "information_insufficient":
            if (
                self.value != "信息不足"
                or self.evidence_record_id is not None
                or self.evidence_content_sha256 is not None
            ):
                raise ValueError("insufficient material provenance must be empty")
        elif not has_provenance:
            raise ValueError("supported material provenance is required")
        return self


class StyleEligibilityInputV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["3-style-eligibility-input"] = "3-style-eligibility-input"
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
    material: StyleMaterialFactV3
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
    def require_consistent_references(self) -> "StyleEligibilityInputV3":
        if self.main_image not in self.images:
            raise ValueError("main image must belong to images")
        if self.transform_authorization_status == "none":
            if any(
                (
                    self.transform_authorization_id,
                    self.transform_authorization_expires_at,
                    self.transform_authorization_revoked_at,
                )
            ):
                raise ValueError("authorization snapshot is inconsistent")
        elif (
            self.transform_authorization_id is None
            or self.transform_authorization_expires_at is None
        ):
            raise ValueError("authorization snapshot is inconsistent")
        if (self.transform_authorization_status == "revoked") != (
            self.transform_authorization_revoked_at is not None
        ):
            raise ValueError("authorization snapshot is inconsistent")
        return self


class StyleWarehouseDeliveryV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["style-warehouse-delivery-v2"] = "style-warehouse-delivery-v2"
    handoff_id: UUID
    idempotency_key: IdempotencyKey
    payload_sha256: Sha256Hex
    payload: StyleEligibilityInputV3

    @model_validator(mode="after")
    def bind_delivery(self) -> "StyleWarehouseDeliveryV2":
        if self.payload_sha256 != self.payload.content_sha256:
            raise ValueError("payload digest must match")
        expected = f"style-v3:{self.payload.tenant_id}:{self.payload.source_classification_id}"
        if self.idempotency_key != expected:
            raise ValueError("idempotency key must match payload source")
        return self


class StyleWarehouseDecisionReceiptV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["style-warehouse-decision-receipt-v2"] = (
        "style-warehouse-decision-receipt-v2"
    )
    decision_receipt_id: UUID
    handoff_id: UUID
    checked_payload_sha256: Sha256Hex
    decision: Literal["accepted", "rejected"]
    rejection_code: str | None = Field(default=None, max_length=64)
    generation_job_id: None = None
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def require_decision_fields(self) -> "StyleWarehouseDecisionReceiptV2":
        if (self.decision == "rejected") != (self.rejection_code is not None):
            raise ValueError("rejection code must match decision")
        return self


def canonical_style_eligibility_v3_sha256(value: StyleEligibilityInputV3) -> str:
    body = value.model_dump(
        mode="json", exclude={"content_sha256", "eligibility_input_version", "created_at"}
    )
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()
