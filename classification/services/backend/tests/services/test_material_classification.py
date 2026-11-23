from uuid import UUID

import pytest

from product_engine.services.material_classification import (
    classify_album_category,
    classify_trainer_label,
)


@pytest.mark.parametrize(
    ("input_label", "category_code", "category_label"),
    [
        ("沉香", "agarwood", "沉香"),
        ("绿松", "turquoise", "绿松石"),
        ("绿松石", "turquoise", "绿松石"),
        ("南红", "nanhong", "南红"),
        ("和田玉", "hetian_jade", "和田玉"),
        ("彩宝", "colored_gemstone", "彩宝"),
        ("彩色宝石", "colored_gemstone", "彩宝"),
        ("翡翠", "jadeite", "翡翠"),
    ],
)
def test_album_aliases_become_reviewable_candidates(
    input_label: str,
    category_code: str,
    category_label: str,
) -> None:
    decision = classify_album_category(input_label, 0.91)

    assert decision.category_code == category_code
    assert decision.category_label == category_label
    assert decision.decision_status == "candidate"
    assert decision.source == "album_import"
    assert decision.confidence_level == "high"
    assert decision.review_required is True
    assert decision.mall_import_allowed is False
    assert decision.evidence_refs == []


@pytest.mark.parametrize("input_label", ["蜜蜡", "绿色", "其他", "无法判断", " "])
def test_outside_or_ambiguous_album_label_returns_information_insufficient(
    input_label: str,
) -> None:
    decision = classify_album_category(input_label, 0.99)

    assert decision.category_code == "information_insufficient"
    assert decision.category_label == "信息不足"
    assert decision.decision_status == "information_insufficient"
    assert decision.source == "none"
    assert decision.confidence_level is None
    assert decision.review_required is True
    assert decision.mall_import_allowed is False


def test_album_confidence_is_explanatory_and_low_confidence_falls_back() -> None:
    medium = classify_album_category("南红", 0.70)
    low = classify_album_category("南红", 0.20)

    assert medium.confidence_level == "medium"
    assert medium.decision_status == "candidate"
    assert medium.review_required is True
    assert low.category_code == "information_insufficient"
    assert low.confidence_level is None
    assert low.decision_status == "information_insufficient"
    assert low.review_required is True


def test_supported_trainer_label_is_confirmed_with_version_evidence() -> None:
    version_id = UUID("11111111-1111-4111-8111-111111111111")

    decision = classify_trainer_label(
        "绿松",
        version_id=version_id,
        template_code="LOCAL-GREEN-BRACELET",
    )

    assert decision.category_code == "turquoise"
    assert decision.category_label == "绿松石"
    assert decision.decision_status == "human_confirmed"
    assert decision.source == "trainer_confirmed"
    assert decision.confidence_level == "high"
    assert decision.review_required is False
    assert decision.mall_import_allowed is True
    assert [item.model_dump(mode="json") for item in decision.evidence_refs] == [
        {
            "evidence_type": "training_version",
            "reference_id": "LOCAL-GREEN-BRACELET:11111111-1111-4111-8111-111111111111",
        }
    ]


def test_out_of_scope_trainer_label_remains_information_insufficient() -> None:
    decision = classify_trainer_label(
        "蜜蜡",
        version_id=UUID("11111111-1111-4111-8111-111111111111"),
        template_code="LOCAL-AMBER",
    )

    assert decision.category_code == "information_insufficient"
    assert decision.decision_status == "information_insufficient"
    assert decision.mall_import_allowed is False
    assert decision.evidence_refs == []
