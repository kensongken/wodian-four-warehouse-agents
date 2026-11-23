import json
from pathlib import Path
from uuid import UUID

from product_engine.services.material_classification import (
    ClassificationDecision,
    classify_album_category,
    classify_trainer_label,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "fixtures"
    / "v0.1"
    / "six-category-classification-cases.json"
)


def test_eight_case_fixture_replays_six_categories_and_two_safe_fallbacks() -> None:
    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert len(cases) == 8
    decisions = [ClassificationDecision.model_validate(case["decision"]) for case in cases]
    assert {
        decision.category_code
        for decision in decisions
        if decision.category_code != "information_insufficient"
    } == {
        "agarwood",
        "turquoise",
        "nanhong",
        "hetian_jade",
        "colored_gemstone",
        "jadeite",
    }
    assert sum(decision.category_code == "information_insufficient" for decision in decisions) == 2
    assert all(
        not decision.mall_import_allowed
        for decision in decisions
        if decision.decision_status != "human_confirmed"
    )

    for case, expected in zip(cases, decisions, strict=True):
        source_input = case["input"]
        if source_input["source"] == "album":
            actual = classify_album_category(
                source_input["category"],
                source_input["classification_confidence"],
            )
        else:
            actual = classify_trainer_label(
                source_input["material_label"],
                version_id=UUID(source_input["version_id"]),
                template_code=source_input["template_code"],
            )
        assert actual == expected, case["case_id"]
