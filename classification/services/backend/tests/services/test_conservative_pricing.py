import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from product_engine.services.conservative_pricing import (
    ConservativePricingFailure,
    PriceAnchor,
    build_conservative_price_candidate,
)
from product_engine.services.product_passport_drafts import build_product_passport_draft
from product_engine.services.training_label_versions import TrainingLabelVersion


def confirmed_record(**updates: object) -> TrainingLabelVersion:
    record = TrainingLabelVersion.model_validate(
        {
            "sha256": "a" * 64,
            "main_image_template_code": "LOCAL-GREEN-BRACELET",
            "product_type": "first_sale_bead",
            "inventory_status": "in_stock",
            "material_label": "绿松",
            "product_form": "手串",
            "visual_observations": ["绿色调明显"],
            "selling_points": ["近景实拍展示珠体轮廓"],
            "specification_note": "珠径 5 mm；尺寸由训练员测量",
            "quality_note": "图片可见颜色分布较均匀，仅为观察记录",
            "defect_note": "图片可见轻微表面纹理",
            "price_range": None,
            "service_rules": [],
            "version_id": uuid4(),
            "template_code": "LOCAL-GREEN-BRACELET",
            "version": 3,
            "previous_version_id": uuid4(),
            "rollback_target_version_id": None,
            "change_source": "trainer_confirmed",
            "material_evidence": "trainer_confirmed",
            "created_at": datetime(2026, 8, 30, 11, 0, tzinfo=UTC),
            "can_rollback": True,
        }
    )
    return record.model_copy(update=updates)


def anchor(**updates: object) -> PriceAnchor:
    values: dict[str, object] = {
        "standard_code": "PRICE-TURQUOISE-BRACELET-001",
        "category_code": "turquoise",
        "form": "手串",
        "specification_summary": "珠径约 5 mm",
        "quality_summary": "颜色分布较均匀",
        "defect_summary": "允许轻微表面纹理",
        "currency": "CNY",
        "minimum": Decimal("1200"),
        "maximum": Decimal("1500"),
        "basis": "训练员录入的同品类同形态价格标准",
        "evidence_level": "merchant_declared",
        "sample_size": 1,
        "anchor_version": 1,
        "confirmation_source": "trainer_confirmed",
        "confirmed_by": "synthetic-trainer-001",
        "confirmed_at": datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    }
    values.update(updates)
    return PriceAnchor.model_validate(values)


def test_matching_single_anchor_returns_low_confidence_candidate_range() -> None:
    draft = build_product_passport_draft(confirmed_record())

    candidate = build_conservative_price_candidate(draft, anchor())

    assert candidate.minimum == Decimal("1200")
    assert candidate.maximum == Decimal("1500")
    assert candidate.confidence == "low_single_anchor"
    assert candidate.sample_size == 1
    assert candidate.recommended_exact_price is None
    assert candidate.candidate_only is True
    assert candidate.automatic_publication_allowed is False


@pytest.mark.parametrize(
    ("updates", "error_code"),
    [
        ({"category_code": "nanhong"}, "price_anchor_category_mismatch"),
        ({"form": "吊坠"}, "price_anchor_form_mismatch"),
    ],
)
def test_mismatched_anchor_fails_closed(updates: dict[str, object], error_code: str) -> None:
    draft = build_product_passport_draft(confirmed_record())

    with pytest.raises(ConservativePricingFailure, match=error_code):
        build_conservative_price_candidate(draft, anchor(**updates))


def test_incomplete_product_factors_do_not_receive_a_price_candidate() -> None:
    draft = build_product_passport_draft(
        confirmed_record(specification_note="", quality_note="", defect_note="")
    )

    with pytest.raises(
        ConservativePricingFailure,
        match="product_price_factors_incomplete",
    ):
        build_conservative_price_candidate(draft, anchor())

    whitespace_draft = build_product_passport_draft(
        confirmed_record(specification_note="   ", quality_note="   ", defect_note="")
    )
    with pytest.raises(
        ConservativePricingFailure,
        match="product_price_factors_incomplete",
    ):
        build_conservative_price_candidate(whitespace_draft, anchor())


def test_anchor_accepts_quality_or_defect_and_rejects_blank_text() -> None:
    quality_only = anchor(defect_summary="")
    defect_only = anchor(quality_summary="")

    assert quality_only.quality_summary
    assert quality_only.defect_summary == ""
    assert defect_only.quality_summary == ""
    assert defect_only.defect_summary

    with pytest.raises(ValidationError):
        anchor(specification_summary="   ")
    with pytest.raises(ValidationError):
        anchor(quality_summary=" ", defect_summary=" ")


def test_anchor_requires_explicit_human_confirmation_provenance() -> None:
    with pytest.raises(ValidationError):
        anchor(confirmed_by="   ")
    with pytest.raises(ValidationError):
        anchor(confirmation_source="system_generated")

    candidate = build_conservative_price_candidate(
        build_product_passport_draft(confirmed_record()),
        anchor(),
    )
    assert candidate.anchor_version == 1
    assert candidate.confirmation_source == "trainer_confirmed"
    assert candidate.confirmed_by == "synthetic-trainer-001"


def test_invalid_anchor_range_and_mutated_unsafe_basis_fail_closed() -> None:
    with pytest.raises(ValidationError):
        anchor(minimum=Decimal("1500"), maximum=Decimal("1500"))

    draft = build_product_passport_draft(confirmed_record())
    unsafe_anchor = anchor().model_copy(update={"basis": "官方认证正品固定售价"})
    with pytest.raises(
        ConservativePricingFailure,
        match="forbidden_unsupported_claim",
    ):
        build_conservative_price_candidate(draft, unsafe_anchor)


@pytest.mark.parametrize(
    "updates",
    [
        {"minimum": Decimal("1500"), "maximum": Decimal("1500")},
        {"confirmed_by": "   "},
        {"confirmed_at": datetime(2026, 8, 30, 12, 0)},
        {"specification_summary": "   "},
    ],
)
def test_mutated_invalid_anchor_is_revalidated_at_builder_boundary(
    updates: dict[str, object],
) -> None:
    draft = build_product_passport_draft(confirmed_record())
    mutated_anchor = anchor().model_copy(update=updates)

    with pytest.raises(ConservativePricingFailure, match="invalid_price_anchor"):
        build_conservative_price_candidate(draft, mutated_anchor)


def test_synthetic_price_candidate_fixture_matches_runtime() -> None:
    draft = build_product_passport_draft(confirmed_record())
    candidate = build_conservative_price_candidate(draft, anchor())
    fixture = (
        Path(__file__).resolve().parents[4]
        / "fixtures"
        / "v0.1"
        / "conservative-price-candidate.json"
    )

    assert candidate.model_dump(mode="json") == json.loads(fixture.read_text(encoding="utf-8"))
