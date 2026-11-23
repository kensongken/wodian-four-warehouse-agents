"""Deterministic shadow risk gate; never confirms a foreground automatically."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from string import hexdigits
from typing import Literal, cast

from PIL import Image, ImageChops, ImageFilter, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import RiskSignals, RiskVerdict
from opc_style.domain.product_foreground import (
    ExtractedForeground,
    ForegroundExtractionRequest,
    ForegroundQuality,
)


@dataclass(frozen=True)
class RiskThresholds:
    """Calibration-owned limits frozen before acceptance samples are scored."""

    minimum_model_iou: float = 0.85
    maximum_area_delta_ratio: float = 0.15
    maximum_component_count: int = 20
    minimum_fine_structure_ratio: float = 0.90
    config_sha256: str = "f" * 64


_LOCAL_STAGE_POLICY = {
    "maximum_area_delta_ratio": 0.15,
    "maximum_component_count": 20,
    "minimum_fine_structure_ratio": 0.90,
    "minimum_model_iou": 0.85,
    "policy": "local-stage-disagreement-v1",
}
_LOCAL_STAGE_POLICY_SHA256 = sha256(
    json.dumps(
        _LOCAL_STAGE_POLICY,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
_LOCAL_STAGE_EVALUATOR_SHA256 = sha256(
    b"deterministic-local-stage-evaluator-v1"
).hexdigest()


class LocalStageDisagreementEvaluator:
    """Turn sealed pipeline stages into a deterministic second-opinion gate."""

    model_manifest_sha256 = _LOCAL_STAGE_EVALUATOR_SHA256
    thresholds = RiskThresholds(
        minimum_model_iou=0.85,
        maximum_area_delta_ratio=0.15,
        maximum_component_count=20,
        minimum_fine_structure_ratio=0.90,
        config_sha256=_LOCAL_STAGE_POLICY_SHA256,
    )

    def evaluate(
        self,
        request: ForegroundExtractionRequest,
        baseline: ExtractedForeground,
        quality: ForegroundQuality,
    ) -> RiskSignals:
        del request
        evidence_complete = bool(
            baseline.pipeline_manifest_sha256
            and baseline.coarse_mask_png
            and len(baseline.stage_receipts) == 2
        )
        return RiskSignals(
            coarse_refined_iou=None,
            area_delta_ratio=None,
            touches_edge=quality.touches_edge,
            component_count=0,
            fine_structure_ratio=0.0,
            high_risk_components=(),
            evidence_complete=evidence_complete,
        )


@dataclass(frozen=True)
class ShadowRiskAssessment:
    verdict: RiskVerdict
    reason_codes: tuple[str, ...]
    config_sha256: str


DisagreementClassification = Literal[
    "no_disagreement",
    "coarse_only_detached_dominant",
    "coarse_only_boundary_dominant",
    "refined_only_dominant",
    "mixed_disagreement",
]


@dataclass(frozen=True)
class StageDisagreementProfile:
    """Directional mask facts; classifications are candidates, not truth labels."""

    classification: DisagreementClassification
    disagreement_ratio: float
    coarse_only_ratio: float
    refined_only_ratio: float
    coarse_only_detached_ratio: float


def _validate(thresholds: RiskThresholds) -> None:
    if (
        not 0 <= thresholds.minimum_model_iou <= 1
        or not 0 <= thresholds.maximum_area_delta_ratio <= 1
        or thresholds.maximum_component_count <= 0
        or not 0 <= thresholds.minimum_fine_structure_ratio <= 1
        or len(thresholds.config_sha256) != 64
        or any(character not in hexdigits for character in thresholds.config_sha256)
    ):
        raise DomainRuleError("foreground_risk_thresholds_invalid")


def assess_shadow_risk(
    signals: RiskSignals, thresholds: RiskThresholds
) -> ShadowRiskAssessment:
    """Return a conservative technical verdict plus stable reason codes."""
    _validate(thresholds)
    if (
        not signals.evidence_complete
        or signals.coarse_refined_iou is None
        or signals.area_delta_ratio is None
    ):
        return ShadowRiskAssessment(
            verdict="blocked",
            reason_codes=("shadow_evidence_incomplete",),
            config_sha256=thresholds.config_sha256,
        )

    reasons: list[str] = []
    if signals.coarse_refined_iou < thresholds.minimum_model_iou:
        reasons.append("model_disagreement")
    if signals.area_delta_ratio > thresholds.maximum_area_delta_ratio:
        reasons.append("abnormal_area_delta")
    if signals.touches_edge:
        reasons.append("mask_touches_edge")
    if signals.component_count > thresholds.maximum_component_count:
        reasons.append("excessive_components")
    if signals.fine_structure_ratio < thresholds.minimum_fine_structure_ratio:
        reasons.append("fine_structure_risk")
    reasons.extend(
        f"high_risk_component:{component}"
        for component in sorted(set(signals.high_risk_components))
    )
    return ShadowRiskAssessment(
        verdict="human_review_required" if reasons else "auto_eligible",
        reason_codes=tuple(reasons),
        config_sha256=thresholds.config_sha256,
    )


def evaluate_shadow_risk(
    signals: RiskSignals, thresholds: RiskThresholds
) -> RiskVerdict:
    """Compatibility helper for callers that need only the verdict literal."""
    return assess_shadow_risk(signals, thresholds).verdict


def _binary_image(content: bytes, width: int, height: int) -> Image.Image:
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            if image.format != "PNG" or image.mode != "L":
                raise DomainRuleError("foreground_stage_comparison_invalid")
            if image.size != (width, height):
                raise DomainRuleError("foreground_stage_comparison_invalid")
            return image.point(lambda value: 255 if value >= 128 else 0)
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise DomainRuleError("foreground_stage_comparison_invalid") from None


def _binary_pixels(content: bytes, width: int, height: int) -> bytearray:
    pixels = cast(
        tuple[int, ...],
        _binary_image(content, width, height).get_flattened_data(),
    )
    return bytearray(1 if value else 0 for value in pixels)


def _component_count(active: bytearray, width: int, height: int) -> int:
    remaining = bytearray(active)
    components = 0
    for start in range(width * height):
        if not remaining[start]:
            continue
        components += 1
        remaining[start] = 0
        stack = [start]
        while stack:
            current = stack.pop()
            x, y = current % width, current // width
            for neighbor_y in range(max(0, y - 1), min(height, y + 2)):
                for neighbor_x in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = neighbor_y * width + neighbor_x
                    if remaining[neighbor]:
                        remaining[neighbor] = 0
                        stack.append(neighbor)
    return components


def compare_extraction_stages(
    coarse_mask_png: bytes,
    refined_alpha_png: bytes,
    width: int,
    height: int,
    *,
    high_risk_components: tuple[str, ...],
    evidence_complete: bool,
) -> RiskSignals:
    """Compute conservative, provider-neutral disagreement from two masks."""
    if width <= 0 or height <= 0:
        raise DomainRuleError("foreground_stage_comparison_invalid")
    coarse = _binary_pixels(coarse_mask_png, width, height)
    refined = _binary_pixels(refined_alpha_png, width, height)
    coarse_count = sum(coarse)
    refined_count = sum(refined)
    intersection = sum(
        before and after for before, after in zip(coarse, refined, strict=True)
    )
    union = sum(before or after for before, after in zip(coarse, refined, strict=True))
    iou = intersection / union if union else None
    area_delta = (
        abs(refined_count - coarse_count) / coarse_count if coarse_count else None
    )
    active_indices = [index for index, value in enumerate(refined) if value]
    touches_edge = any(
        index < width
        or index >= width * (height - 1)
        or index % width == 0
        or index % width == width - 1
        for index in active_indices
    )
    return RiskSignals(
        coarse_refined_iou=iou,
        area_delta_ratio=area_delta,
        touches_edge=touches_edge,
        component_count=_component_count(refined, width, height),
        fine_structure_ratio=(intersection / coarse_count if coarse_count else 0.0),
        high_risk_components=high_risk_components,
        evidence_complete=evidence_complete,
    )


def profile_extraction_stage_disagreement(
    coarse_mask_png: bytes,
    refined_alpha_png: bytes,
    width: int,
    height: int,
) -> StageDisagreementProfile:
    """Describe stage differences without treating either mask as ground truth."""
    if width <= 0 or height <= 0:
        raise DomainRuleError("foreground_stage_comparison_invalid")
    coarse = _binary_image(coarse_mask_png, width, height)
    refined = _binary_image(refined_alpha_png, width, height)
    coarse_only = ImageChops.subtract(coarse, refined)
    refined_only = ImageChops.subtract(refined, coarse)
    union_count = ImageChops.lighter(coarse, refined).histogram()[255]
    coarse_only_count = coarse_only.histogram()[255]
    refined_only_count = refined_only.histogram()[255]
    disagreement_count = coarse_only_count + refined_only_count
    if disagreement_count == 0:
        return StageDisagreementProfile(
            classification="no_disagreement",
            disagreement_ratio=0.0,
            coarse_only_ratio=0.0,
            refined_only_ratio=0.0,
            coarse_only_detached_ratio=0.0,
        )

    radius = max(1, min(3, round(min(width, height) * 0.001)))
    expanded_refined = refined.filter(ImageFilter.MaxFilter(radius * 2 + 1))
    detached = ImageChops.multiply(coarse_only, ImageChops.invert(expanded_refined))
    detached_count = detached.histogram()[255]
    detached_ratio = detached_count / coarse_only_count if coarse_only_count else 0.0
    if coarse_only_count > refined_only_count * 1.5:
        classification: DisagreementClassification = (
            "coarse_only_detached_dominant"
            if detached_ratio >= 0.65
            else "coarse_only_boundary_dominant"
        )
    elif refined_only_count > coarse_only_count * 1.5:
        classification = "refined_only_dominant"
    else:
        classification = "mixed_disagreement"
    denominator = union_count or width * height
    return StageDisagreementProfile(
        classification=classification,
        disagreement_ratio=disagreement_count / denominator,
        coarse_only_ratio=coarse_only_count / denominator,
        refined_only_ratio=refined_only_count / denominator,
        coarse_only_detached_ratio=detached_ratio,
    )
