from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from product_engine.services.conservative_pricing import (
    PriceAnchor,
    build_conservative_price_candidate,
)
from product_engine.services.internal_product_content_package import (
    InternalProductContentPackageFailure,
    build_internal_product_content_package,
)
from product_engine.services.product_passport_drafts import build_product_passport_draft
from product_engine.services.training_label_versions import (
    ServiceRuleInput,
    TrainingLabelVersion,
)


def confirmed_record() -> TrainingLabelVersion:
    return TrainingLabelVersion.model_validate(
        {
            "sha256": "b" * 64,
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
            "service_rules": [
                ServiceRuleInput(
                    service_code="scan_identity",
                    eligibility="购买后可用",
                    customer_summary="购买后可扫码进入身份与服务页面",
                )
            ],
            "version_id": uuid4(),
            "template_code": "LOCAL-GREEN-BRACELET",
            "version": 4,
            "previous_version_id": uuid4(),
            "rollback_target_version_id": None,
            "change_source": "trainer_confirmed",
            "material_evidence": "trainer_confirmed",
            "created_at": datetime(2026, 8, 30, 13, 0, tzinfo=UTC),
            "can_rollback": True,
        }
    )


def price_anchor() -> PriceAnchor:
    return PriceAnchor(
        standard_code="PRICE-TURQUOISE-BRACELET-001",
        category_code="turquoise",
        form="手串",
        specification_summary="珠径约 5 mm",
        quality_summary="颜色分布较均匀",
        defect_summary="允许轻微表面纹理",
        minimum=Decimal("1200"),
        maximum=Decimal("1500"),
        basis="训练员录入的同品类同形态价格标准",
        anchor_version=1,
        confirmation_source="trainer_confirmed",
        confirmed_by="synthetic-trainer-001",
        confirmed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )


def test_internal_package_keeps_conservative_price_pending_human_confirmation() -> None:
    draft = build_product_passport_draft(confirmed_record())
    price_candidate = build_conservative_price_candidate(draft, price_anchor())

    package = build_internal_product_content_package(draft, price_candidate, price_anchor())

    assert package.conservative_price_candidate == price_candidate
    assert package.price_review_status == "pending_human_confirmation"
    assert package.publication_status == "blocked"
    assert package.sales_journey_usage == "preview_only"
    assert package.automatic_publication_allowed is False
    assert "price_range_missing" in [blocker.code for blocker in package.draft.blockers]
    assert any(
        claim.field_path == "conservative_price_candidate"
        and claim.evidence_level == "merchant_declared"
        for claim in package.content_evidence.claims
    )


@pytest.mark.parametrize(
    ("updates", "error_code"),
    [
        ({"template_code": "OTHER"}, "price_candidate_template_mismatch"),
        ({"source_version": 99}, "price_candidate_source_version_mismatch"),
        ({"category_code": "nanhong"}, "price_candidate_category_mismatch"),
        ({"form": "吊坠"}, "price_candidate_form_mismatch"),
    ],
)
def test_internal_package_rejects_candidate_from_another_product(
    updates: dict[str, object],
    error_code: str,
) -> None:
    draft = build_product_passport_draft(confirmed_record())
    price_candidate = build_conservative_price_candidate(draft, price_anchor()).model_copy(
        update=updates
    )

    with pytest.raises(InternalProductContentPackageFailure, match=error_code):
        build_internal_product_content_package(draft, price_candidate, price_anchor())


@pytest.mark.parametrize(
    "updates",
    [
        {"automatic_publication_allowed": True},
        {"minimum": Decimal("1500"), "maximum": Decimal("1500")},
        {"confirmed_by": "   "},
        {"basis": "官方认证正品固定售价"},
        {"minimum": Decimal("1300")},
        {"standard_code": "FABRICATED", "anchor_version": 999},
    ],
)
def test_internal_package_revalidates_mutated_candidate_boundary(
    updates: dict[str, object],
) -> None:
    draft = build_product_passport_draft(confirmed_record())
    price_candidate = build_conservative_price_candidate(draft, price_anchor()).model_copy(
        update=updates
    )

    with pytest.raises(
        InternalProductContentPackageFailure,
        match="invalid_conservative_price_candidate",
    ):
        build_internal_product_content_package(draft, price_candidate, price_anchor())


def test_internal_package_revalidates_mutated_draft_boundary() -> None:
    draft = build_product_passport_draft(confirmed_record())
    mutated_draft = draft.model_copy(update={"publication_status": "ready"})
    price_candidate = build_conservative_price_candidate(draft, price_anchor())

    with pytest.raises(
        InternalProductContentPackageFailure,
        match="invalid_product_passport_draft",
    ):
        build_internal_product_content_package(mutated_draft, price_candidate, price_anchor())


def test_internal_package_requires_original_missing_price_blocker() -> None:
    draft = build_product_passport_draft(confirmed_record())
    mutated_draft = draft.model_copy(update={"blockers": []})
    price_candidate = build_conservative_price_candidate(draft, price_anchor())

    with pytest.raises(
        InternalProductContentPackageFailure,
        match="formal_price_review_blocker_missing",
    ):
        build_internal_product_content_package(mutated_draft, price_candidate, price_anchor())


@pytest.mark.parametrize(
    "updates",
    [
        {"template_code": " LOCAL-GREEN-BRACELET "},
        {"form": " 手串 "},
    ],
)
def test_internal_package_requires_exact_unmodified_identity_text(
    updates: dict[str, object],
) -> None:
    draft = build_product_passport_draft(confirmed_record())
    price_candidate = build_conservative_price_candidate(draft, price_anchor()).model_copy(
        update=updates
    )

    with pytest.raises(InternalProductContentPackageFailure):
        build_internal_product_content_package(draft, price_candidate, price_anchor())
