"""Fail-closed spatial evidence for actual product occlusion.

This module deliberately does not infer an expected product shape from one image.
It only decides from three same-size masks supplied by upstream components:

* expected product support (where product pixels should exist),
* visible product pixels,
* carrier or covering-object pixels.

Without a reviewed expected mask, the result always stays in manual review.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError

OcclusionState = Literal["present", "absent", "unclear"]
ReviewAction = Literal["continue", "manual_review", "block"]

_MASK_INVALID = "occlusion_geometry_mask_invalid"
_EVIDENCE_INVALID = "occlusion_geometry_evidence_invalid"


@dataclass(frozen=True)
class OcclusionGeometryThresholds:
    """Conservative thresholds; changing them requires a newly frozen evaluation."""

    minimum_missing_product_ratio: float = 0.01
    maximum_clear_missing_ratio: float = 0.005
    minimum_carrier_explanation_ratio: float = 0.60
    maximum_visible_outside_expected_ratio: float = 0.02
    minimum_overlap_pixels: int = 16


@dataclass(frozen=True)
class OcclusionGeometryEvidence:
    """Measured spatial facts and the only safe action they currently support."""

    expected_product_pixels: int
    visible_product_pixels: int
    missing_expected_pixels: int
    carrier_pixels: int
    carrier_over_missing_pixels: int
    visible_outside_expected_pixels: int
    missing_product_ratio: float
    carrier_explanation_ratio: float
    visible_outside_expected_ratio: float
    actual_occlusion: OcclusionState
    review_action: ReviewAction
    reason: str


def assess_occlusion_geometry(
    *,
    expected_product_mask_png: bytes,
    visible_product_mask_png: bytes,
    carrier_mask_png: bytes,
    width: int,
    height: int,
    expected_mask_human_confirmed: bool,
    visible_mask_reliable: bool,
    carrier_mask_reliable: bool,
    thresholds: OcclusionGeometryThresholds | None = None,
) -> OcclusionGeometryEvidence:
    """Assess actual coverage without equating carrier presence with occlusion."""
    if width <= 0 or height <= 0:
        raise DomainRuleError(_EVIDENCE_INVALID)
    if any(
        type(value) is not bool
        for value in (
            expected_mask_human_confirmed,
            visible_mask_reliable,
            carrier_mask_reliable,
        )
    ):
        raise DomainRuleError(_EVIDENCE_INVALID)
    limits = thresholds or OcclusionGeometryThresholds()
    _validate_thresholds(limits)

    expected = _load_binary_mask(expected_product_mask_png, width, height)
    visible = _load_binary_mask(visible_product_mask_png, width, height)
    carrier = _load_binary_mask(carrier_mask_png, width, height)
    expected_values = expected.tobytes()
    visible_values = visible.tobytes()
    carrier_values = carrier.tobytes()

    expected_pixels = visible_pixels = missing_pixels = carrier_pixels = 0
    carrier_over_missing = visible_outside_expected = 0
    for expected_value, visible_value, carrier_value in zip(
        expected_values, visible_values, carrier_values, strict=True
    ):
        expected_on = expected_value > 0
        visible_on = visible_value > 0
        carrier_on = carrier_value > 0
        expected_pixels += expected_on
        visible_pixels += visible_on
        carrier_pixels += carrier_on
        missing = expected_on and not visible_on
        missing_pixels += missing
        carrier_over_missing += missing and carrier_on
        visible_outside_expected += visible_on and not expected_on

    if expected_pixels == 0 or visible_pixels == 0:
        raise DomainRuleError(_MASK_INVALID)
    missing_ratio = missing_pixels / expected_pixels
    explanation_ratio = carrier_over_missing / missing_pixels if missing_pixels else 0.0
    outside_ratio = visible_outside_expected / visible_pixels

    evidence_ready = (
        expected_mask_human_confirmed
        and visible_mask_reliable
        and carrier_mask_reliable
        and outside_ratio <= limits.maximum_visible_outside_expected_ratio
    )
    if not evidence_ready:
        state: OcclusionState = "unclear"
        action: ReviewAction = "manual_review"
        reason = "spatial_evidence_not_reviewed_or_masks_unreliable"
    elif (
        missing_ratio >= limits.minimum_missing_product_ratio
        and explanation_ratio >= limits.minimum_carrier_explanation_ratio
        and carrier_over_missing >= limits.minimum_overlap_pixels
    ):
        state = "present"
        action = "block"
        reason = "reviewed_missing_product_region_is_explained_by_carrier_overlap"
    elif missing_ratio <= limits.maximum_clear_missing_ratio:
        state = "absent"
        action = "continue"
        reason = "reviewed_expected_product_region_is_materially_visible"
    else:
        state = "unclear"
        action = "manual_review"
        reason = "missing_product_region_is_not_conclusively_explained"

    return OcclusionGeometryEvidence(
        expected_product_pixels=expected_pixels,
        visible_product_pixels=visible_pixels,
        missing_expected_pixels=missing_pixels,
        carrier_pixels=carrier_pixels,
        carrier_over_missing_pixels=carrier_over_missing,
        visible_outside_expected_pixels=visible_outside_expected,
        missing_product_ratio=missing_ratio,
        carrier_explanation_ratio=explanation_ratio,
        visible_outside_expected_ratio=outside_ratio,
        actual_occlusion=state,
        review_action=action,
        reason=reason,
    )


def _load_binary_mask(content: bytes, width: int, height: int) -> Image.Image:
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            if image.format != "PNG" or image.mode != "L" or image.size != (width, height):
                raise DomainRuleError(_MASK_INVALID)
            return image.point(lambda value: 255 if value >= 128 else 0, mode="L")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise DomainRuleError(_MASK_INVALID) from None


def _validate_thresholds(thresholds: OcclusionGeometryThresholds) -> None:
    ratios = (
        thresholds.minimum_missing_product_ratio,
        thresholds.maximum_clear_missing_ratio,
        thresholds.minimum_carrier_explanation_ratio,
        thresholds.maximum_visible_outside_expected_ratio,
    )
    if (
        any(not 0 <= value <= 1 for value in ratios)
        or thresholds.maximum_clear_missing_ratio
        >= thresholds.minimum_missing_product_ratio
        or thresholds.minimum_overlap_pixels <= 0
    ):
        raise DomainRuleError(_EVIDENCE_INVALID)
