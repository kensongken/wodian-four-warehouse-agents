from uuid import UUID

import pytest
from pydantic import ValidationError

from product_engine.contracts.material_evidence import (
    MaterialEvidenceRegistrationCommand,
    MaterialEvidenceRevocationCommand,
    canonical_material_evidence_content_sha256,
    canonical_material_evidence_request_sha256,
    canonical_material_evidence_revocation_sha256,
)

REQUEST_1 = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_2 = UUID("22222222-2222-4222-8222-222222222222")
BASE_ID = UUID("33333333-3333-4333-8333-333333333333")
NEW_BASE_ID = UUID("44444444-4444-4444-8444-444444444444")
EVIDENCE_ID = UUID("55555555-5555-4555-8555-555555555555")


def registration(**updates: object) -> MaterialEvidenceRegistrationCommand:
    values: dict[str, object] = {
        "request_id": REQUEST_1,
        "product_code": "BCNHDIY914",
        "base_classification_id": BASE_ID,
        "material_label": "南红",
        "claim_basis": "document_supported",
        "evidence_kind": "supplier_label",
        "evidence_ref": "material-evidence:first",
        "evidence_sha256": "a" * 64,
        "source_summary": "供应商标签，编号尾号 1234",
        "acknowledge_not_authentication": False,
    }
    values.update(updates)
    return MaterialEvidenceRegistrationCommand.model_validate(values)


def test_merchant_declaration_requires_non_authentication_acknowledgement() -> None:
    with pytest.raises(
        ValidationError,
        match="material_declaration_acknowledgement_required",
    ):
        registration(
            claim_basis="merchant_declared",
            evidence_kind="merchant_declaration",
            evidence_sha256=None,
            acknowledge_not_authentication=False,
        )


def test_document_and_merchant_claims_have_distinct_digests() -> None:
    document = registration(
        claim_basis="document_supported",
        evidence_kind="supplier_label",
        evidence_ref="evidence:document:1",
        evidence_sha256="a" * 64,
    )
    declaration = registration(
        claim_basis="merchant_declared",
        evidence_kind="merchant_declaration",
        evidence_ref="evidence:declaration:1",
        evidence_sha256=None,
        acknowledge_not_authentication=True,
    )

    assert canonical_material_evidence_content_sha256(
        document
    ) != canonical_material_evidence_content_sha256(declaration)


def test_generated_transport_ref_does_not_change_semantic_content_digest() -> None:
    first = registration(evidence_ref="material-evidence:first")
    second = registration(
        request_id=REQUEST_2,
        base_classification_id=NEW_BASE_ID,
        evidence_ref="material-evidence:second",
    )

    assert canonical_material_evidence_request_sha256(
        first
    ) != canonical_material_evidence_request_sha256(second)
    assert canonical_material_evidence_content_sha256(
        first
    ) == canonical_material_evidence_content_sha256(second)


def test_document_attachment_requires_ref_and_sha256_together() -> None:
    with pytest.raises(
        ValidationError,
        match="material_evidence_attachment_incomplete",
    ):
        registration(
            claim_basis="document_supported",
            evidence_kind="purchase_record",
            evidence_ref="evidence:document:2",
            evidence_sha256=None,
        )


def test_claim_basis_and_evidence_kind_must_match() -> None:
    with pytest.raises(ValidationError, match="material_evidence_kind_mismatch"):
        registration(
            claim_basis="merchant_declared",
            evidence_kind="supplier_label",
            acknowledge_not_authentication=True,
        )


def test_revocation_hash_excludes_request_id_but_tracks_reason() -> None:
    first = MaterialEvidenceRevocationCommand(
        request_id=REQUEST_1,
        evidence_id=EVIDENCE_ID,
        reason_code="merchant_withdrawn",
        reason_summary="商家撤回原声明",
    )
    replay = first.model_copy(update={"request_id": REQUEST_2})
    changed = first.model_copy(update={"reason_code": "wrong_product"})

    assert canonical_material_evidence_revocation_sha256(first) == (
        canonical_material_evidence_revocation_sha256(replay)
    )
    assert canonical_material_evidence_revocation_sha256(first) != (
        canonical_material_evidence_revocation_sha256(changed)
    )


def test_registration_contract_is_frozen_and_forbids_extra_fields() -> None:
    command = registration()
    with pytest.raises(ValidationError):
        command.material_label = "绿松石"
    with pytest.raises(ValidationError):
        MaterialEvidenceRegistrationCommand.model_validate(
            {**command.model_dump(mode="json"), "tenant_id": str(REQUEST_2)}
        )
