from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from product_engine.contracts.style_warehouse_v3 import (
    StyleEligibilityInputV3,
    canonical_style_eligibility_v3_sha256,
)


def payload(**material_updates: object) -> StyleEligibilityInputV3:
    asset_id = uuid4()
    material = {
        "value": "绿松石",
        "claim_basis": "merchant_declared",
        "evidence_record_id": uuid4(),
        "evidence_content_sha256": "e" * 64,
    }
    material.update(material_updates)
    value = StyleEligibilityInputV3(
        tenant_id=uuid4(),
        product_code="BCNHDIY914",
        eligibility_input_version=1,
        source_material_package_version=1,
        source_classification_id=uuid4(),
        source_classification_version=2,
        source_classification_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        main_image={
            "asset_id": asset_id,
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        },
        images=(
            {
                "asset_id": asset_id,
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        material=material,
        form={"value": "earring", "evidence": "human_confirmed"},
        transform_authorization_status="none",
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
        content_sha256="0" * 64,
    )
    return value.model_copy(update={"content_sha256": canonical_style_eligibility_v3_sha256(value)})


def test_material_provenance_changes_payload_digest() -> None:
    merchant = payload()
    document = payload(
        claim_basis="document_supported",
        evidence_record_id=merchant.material.evidence_record_id,
    )

    assert merchant.content_sha256 != document.content_sha256


def test_insufficient_material_cannot_carry_evidence_identifiers() -> None:
    with pytest.raises(ValidationError, match="insufficient material provenance"):
        payload(
            value="信息不足",
            claim_basis="information_insufficient",
            evidence_record_id=uuid4(),
            evidence_content_sha256="e" * 64,
        )
