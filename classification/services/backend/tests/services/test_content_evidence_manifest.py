import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from product_engine.services.content_evidence_manifest import (
    ContentEvidenceManifestFailure,
    build_content_evidence_manifest,
)
from product_engine.services.product_passport_drafts import build_product_passport_draft
from product_engine.services.training_label_versions import (
    PriceRangeInput,
    ServiceRuleInput,
    TrainingLabelVersion,
)


def confirmed_record(**updates: object) -> TrainingLabelVersion:
    record = TrainingLabelVersion.model_validate(
        {
            "sha256": "a" * 64,
            "main_image_template_code": "LOCAL-GREEN-BRACELET",
            "product_type": "first_sale_bead",
            "inventory_status": "in_stock",
            "material_label": "绿松",
            "product_form": "手串",
            "visual_observations": ["绿色调明显", "珠体排列成环"],
            "selling_points": ["近景实拍展示珠体轮廓"],
            "specification_note": "珠径 5 mm；尺寸由训练员测量",
            "quality_note": "图片可见颜色分布较均匀，仅为观察记录",
            "defect_note": "图片可见轻微表面纹理",
            "price_range": PriceRangeInput(
                minimum="1200",
                maximum="1500",
                basis="训练员录入的同品类价格标准",
            ),
            "service_rules": [
                ServiceRuleInput(
                    service_code="scan_identity",
                    eligibility="购买后可用",
                    customer_summary="购买后可扫码进入身份与服务页面",
                )
            ],
            "version_id": uuid4(),
            "template_code": "LOCAL-GREEN-BRACELET",
            "version": 3,
            "previous_version_id": uuid4(),
            "rollback_target_version_id": None,
            "change_source": "trainer_confirmed",
            "material_evidence": "trainer_confirmed",
            "created_at": datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
            "can_rollback": True,
        }
    )
    return record.model_copy(update=updates)


def test_visible_and_merchant_claims_are_separated() -> None:
    draft = build_product_passport_draft(confirmed_record())

    manifest = build_content_evidence_manifest(draft)

    claims = {claim.field_path: claim for claim in manifest.claims}
    assert claims["form"].evidence_level == "image_observed"
    assert claims["visual_observations.0"].evidence_level == "image_observed"
    assert claims["quality_note"].evidence_level == "image_observed"
    assert claims["defect_note"].evidence_level == "image_observed"
    assert claims["selling_points.0"].evidence_level == "merchant_declared"
    assert claims["specification_note"].evidence_level == "merchant_declared"
    assert claims["price_advisory"].evidence_level == "merchant_declared"
    assert claims["service_rules.0"].evidence_level == "merchant_declared"
    assert all(
        claim.evidence_refs == ["training-label:LOCAL-GREEN-BRACELET:v3"]
        for claim in manifest.claims
    )


def test_unverified_sensitive_fields_are_withheld_from_claims() -> None:
    draft = build_product_passport_draft(confirmed_record())

    manifest = build_content_evidence_manifest(draft)

    claim_fields = {claim.field_path for claim in manifest.claims}
    assert "material" not in claim_fields
    assert manifest.withheld_fields == [
        "material",
        "authenticity",
        "origin",
        "professional_quality_grade",
        "certificate_conclusion",
    ]
    assert manifest.candidate_only is True
    assert manifest.automatic_publication_allowed is False


def test_mutated_unsafe_draft_fails_closed_before_public_summary() -> None:
    safe_draft = build_product_passport_draft(confirmed_record())
    unsafe_draft = safe_draft.model_copy(update={"specification_note": "官方认证正品并保证升值"})

    with pytest.raises(
        ContentEvidenceManifestFailure,
        match="forbidden_unsupported_claim",
    ):
        build_content_evidence_manifest(unsafe_draft)


def test_missing_optional_business_inputs_do_not_create_fake_claims() -> None:
    draft = build_product_passport_draft(
        confirmed_record(
            specification_note="",
            quality_note="",
            defect_note="",
            price_range=None,
            service_rules=[],
        )
    )

    manifest = build_content_evidence_manifest(draft)

    claim_fields = {claim.field_path for claim in manifest.claims}
    assert "specification_note" not in claim_fields
    assert "quality_note" not in claim_fields
    assert "defect_note" not in claim_fields
    assert "price_advisory" not in claim_fields
    assert not any(field.startswith("service_rules.") for field in claim_fields)


def test_synthetic_evidence_manifest_fixture_matches_runtime() -> None:
    draft = build_product_passport_draft(confirmed_record())
    manifest = build_content_evidence_manifest(draft)
    fixture = (
        Path(__file__).resolve().parents[4] / "fixtures" / "v0.1" / "content-evidence-manifest.json"
    )

    assert manifest.model_dump(mode="json") == json.loads(fixture.read_text(encoding="utf-8"))
