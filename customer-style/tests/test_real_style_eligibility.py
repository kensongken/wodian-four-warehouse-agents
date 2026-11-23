"""One fail-closed policy decides whether a style may reach paid generation."""

import importlib.util
from types import SimpleNamespace

import pytest

from opc_style.domain.real_style_eligibility import assess_real_style


def test_real_style_eligibility_policy_is_available() -> None:
    assert importlib.util.find_spec("opc_style.domain.real_style_eligibility") is not None


def _complete_facts():
    content = {
        "style_origin": "customer_upload",
        "analysis_mode": "real_visual_analysis_proposal",
        "stability": "customer_confirmed",
        "sample_count": 10,
        "palette": ["暖白"],
        "lighting": ["柔和左侧漫射光"],
        "composition": ["主体居中"],
        "backgrounds": ["暖白无缝背景"],
        "tone": [],
        "people_and_props": [],
        "material_treatment": [],
        "typography": [],
        "forbidden_elements": [],
    }
    evidence = [
        SimpleNamespace(field_name=field, field_value=value)
        for field in ("palette", "lighting", "composition", "backgrounds")
        for value in content[field]
    ]
    slots = [
        SimpleNamespace(
            active=True,
            rules={
                "aspect_ratio": "4:5",
                "shot_distance": "close",
                "camera_angle": "front",
                "composition": ["centered"],
            },
        )
    ]
    return content, evidence, slots


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "style_origin",
            "local_demo",
            "real_style_customer_upload_required",
        ),
        (
            "analysis_mode",
            "synthetic_test_only",
            "real_style_real_analysis_required",
        ),
        (
            "stability",
            "draft",
            "real_style_customer_confirmation_required",
        ),
        ("lighting", [], "real_style_lighting_required"),
        ("composition", [], "real_style_composition_required"),
        ("backgrounds", [], "real_style_background_required"),
        ("palette", [], "real_style_palette_required"),
    ],
)
def test_real_style_policy_rejects_each_incomplete_fact(
    field: str, value: object, reason: str
) -> None:
    content, evidence, slots = _complete_facts()
    content[field] = value

    result = assess_real_style(content, evidence, 10, slots)

    assert not result.eligible
    assert reason in result.reason_codes


def test_real_style_policy_requires_ten_current_samples_and_field_evidence() -> None:
    content, evidence, slots = _complete_facts()

    too_few = assess_real_style(content, evidence, 9, slots)
    missing_evidence = assess_real_style(
        content,
        [row for row in evidence if row.field_name != "lighting"],
        10,
        slots,
    )

    assert "real_style_ten_samples_required" in too_few.reason_codes
    assert "real_style_field_evidence_required" in missing_evidence.reason_codes


def test_real_style_policy_requires_complete_active_slot() -> None:
    content, evidence, slots = _complete_facts()
    slots[0].rules["composition"] = []

    result = assess_real_style(content, evidence, 10, slots)

    assert result.reason_codes == ("real_style_complete_slot_required",)


def test_complete_real_style_is_eligible() -> None:
    content, evidence, slots = _complete_facts()

    result = assess_real_style(content, evidence, 10, slots)

    assert result.eligible
    assert result.reason_codes == ()


def test_grouped_real_style_uses_global_library_floor_and_own_evidence_count() -> None:
    """A style group may use 2–8 representatives from a verified 10+ photo library."""
    content, evidence, slots = _complete_facts()
    content["sample_count"] = 2
    content["library_sample_count"] = 12

    result = assess_real_style(content, evidence, 2, slots)

    assert result.eligible
    assert result.reason_codes == ()
