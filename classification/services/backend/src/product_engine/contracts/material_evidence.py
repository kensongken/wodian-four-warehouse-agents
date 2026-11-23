import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from product_engine.contracts.common import ProductCode, Sha256Hex

MaterialClaimBasis = Literal["document_supported", "merchant_declared"]
ClassificationMaterialClaimBasis = Literal[
    "document_supported",
    "merchant_declared",
    "information_insufficient",
]
MaterialEvidenceKind = Literal[
    "inspection_certificate",
    "purchase_record",
    "supplier_label",
    "merchant_declaration",
    "other_document",
]
MaterialEvidenceRevocationReason = Literal[
    "merchant_withdrawn",
    "document_invalidated",
    "wrong_product",
    "other",
]


class MaterialEvidenceRegistrationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    product_code: ProductCode
    base_classification_id: UUID
    material_label: str = Field(min_length=1, max_length=128)
    claim_basis: MaterialClaimBasis
    evidence_kind: MaterialEvidenceKind
    evidence_ref: str = Field(min_length=1, max_length=255)
    evidence_sha256: Sha256Hex | None = None
    source_summary: str = Field(min_length=1, max_length=500)
    acknowledge_not_authentication: bool

    @model_validator(mode="after")
    def validate_evidence_basis(self) -> "MaterialEvidenceRegistrationCommand":
        if self.claim_basis == "merchant_declared":
            if self.evidence_kind != "merchant_declaration":
                raise ValueError("material_evidence_kind_mismatch")
            if self.acknowledge_not_authentication is not True:
                raise ValueError("material_declaration_acknowledgement_required")
            return self

        if self.evidence_kind == "merchant_declaration":
            raise ValueError("material_evidence_kind_mismatch")
        if self.evidence_sha256 is None:
            raise ValueError("material_evidence_attachment_incomplete")
        return self


class MaterialEvidenceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: UUID
    material_label: str = Field(min_length=1, max_length=128)
    claim_basis: MaterialClaimBasis
    evidence_kind: MaterialEvidenceKind
    evidence_content_sha256: Sha256Hex
    revoked: bool = False


class MaterialEvidenceReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    evidence: MaterialEvidenceSnapshot
    classification_id: UUID
    classification_version: int = Field(ge=1)
    disposition: Literal["created", "duplicate"]
    downstream_status: Literal["not_sent"] = "not_sent"


class MaterialEvidenceInsufficientCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    product_code: ProductCode
    base_classification_id: UUID


class MaterialEvidenceInsufficientReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    product_code: ProductCode
    classification_id: UUID
    classification_version: int = Field(ge=1)
    material_claim_basis: Literal["information_insufficient"]
    disposition: Literal["created", "duplicate"]
    downstream_status: Literal["not_sent"] = "not_sent"


class MaterialEvidenceRevocationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    evidence_id: UUID
    reason_code: MaterialEvidenceRevocationReason
    reason_summary: str = Field(min_length=1, max_length=500)


class MaterialEvidenceRevocationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    evidence_id: UUID
    classification_id: UUID
    classification_version: int = Field(ge=1)
    material_claim_basis: Literal["information_insufficient"]
    disposition: Literal["created", "duplicate"]
    downstream_status: Literal["not_sent"] = "not_sent"


def _canonical_sha256(value: BaseModel, *, exclude: set[str]) -> str:
    body = value.model_dump(mode="json", exclude=exclude)
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_material_evidence_request_sha256(
    command: MaterialEvidenceRegistrationCommand,
) -> str:
    return _canonical_sha256(command, exclude={"request_id"})


def canonical_material_evidence_content_sha256(
    command: MaterialEvidenceRegistrationCommand,
) -> str:
    return _canonical_sha256(
        command,
        exclude={"request_id", "base_classification_id", "evidence_ref"},
    )


def canonical_material_evidence_insufficient_sha256(
    command: MaterialEvidenceInsufficientCommand,
) -> str:
    return _canonical_sha256(command, exclude={"request_id"})


def canonical_material_evidence_revocation_sha256(
    command: MaterialEvidenceRevocationCommand,
) -> str:
    return _canonical_sha256(command, exclude={"request_id"})
