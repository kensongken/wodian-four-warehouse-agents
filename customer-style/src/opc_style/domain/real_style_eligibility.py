"""Pure, explainable readiness policy for paid customer-style generation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from opc_style.domain.analyzer import STYLE_ANALYSIS_LIST_FIELDS


@dataclass(frozen=True)
class RealStyleEligibility:
    eligible: bool
    reason_codes: tuple[str, ...]


def assess_real_style(
    content: Mapping[str, Any],
    evidence: Sequence[Any],
    sample_count: int,
    slots: Sequence[Any],
) -> RealStyleEligibility:
    reasons: list[str] = []
    if content.get("style_origin") != "customer_upload":
        reasons.append("real_style_customer_upload_required")
    analysis_mode = content.get("analysis_mode")
    normalized_mode = analysis_mode.lower() if isinstance(analysis_mode, str) else ""
    if (
        "real" not in normalized_mode
        or "synthetic" in normalized_mode
        or "demo" in normalized_mode
    ):
        reasons.append("real_style_real_analysis_required")
    if content.get("stability") != "customer_confirmed":
        reasons.append("real_style_customer_confirmation_required")
    library_sample_count = content.get(
        "library_sample_count", content.get("sample_count")
    )
    if content.get("sample_count") != sample_count or not isinstance(
        library_sample_count, int
    ) or library_sample_count < 10:
        reasons.append("real_style_ten_samples_required")
    if sample_count < 2:
        reasons.append("real_style_two_group_samples_required")

    for field, reason in (
        ("lighting", "real_style_lighting_required"),
        ("composition", "real_style_composition_required"),
        ("backgrounds", "real_style_background_required"),
        ("palette", "real_style_palette_required"),
    ):
        if not _string_values(content.get(field)):
            reasons.append(reason)

    evidenced = {
        (getattr(row, "field_name", None), getattr(row, "field_value", None))
        for row in evidence
    }
    conclusions = {
        (field, value)
        for field in STYLE_ANALYSIS_LIST_FIELDS
        for value in _string_values(content.get(field))
    }
    if not conclusions or not conclusions <= evidenced:
        reasons.append("real_style_field_evidence_required")

    if not any(_complete_active_slot(slot) for slot in slots):
        reasons.append("real_style_complete_slot_required")
    return RealStyleEligibility(not reasons, tuple(reasons))


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _complete_active_slot(slot: object) -> bool:
    if getattr(slot, "active", None) is not True:
        return False
    rules = getattr(slot, "rules", None)
    if not isinstance(rules, dict):
        return False
    return (
        isinstance(rules.get("aspect_ratio"), str)
        and bool(rules["aspect_ratio"].strip())
        and isinstance(rules.get("shot_distance"), str)
        and bool(rules["shot_distance"].strip())
        and isinstance(rules.get("camera_angle"), str)
        and bool(rules["camera_angle"].strip())
        and bool(_string_values(rules.get("composition")))
    )
