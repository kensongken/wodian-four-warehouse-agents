"""Gold-mask calibration for directional foreground disagreement hints."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from PIL import Image, ImageChops, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import (
    ForegroundMetricResult,
    score_foreground_mask,
)
from opc_style.services.foreground_risk_gate import (
    DisagreementClassification,
    StageDisagreementProfile,
    profile_extraction_stage_disagreement,
)

CalibrationVerdict = Literal["supported", "contradicted", "inconclusive"]
CalibrationGateStatus = Literal[
    "insufficient_triads",
    "insufficient_category_coverage",
    "directional_hints_failed",
    "eligible_for_review_reduction",
]


@dataclass(frozen=True)
class DisagreementCalibrationSample:
    """One same-source triad scored against a frozen human gold mask."""

    sample_id: str
    category: str
    classification: DisagreementClassification
    verdict: CalibrationVerdict
    coarse_only_gold_fraction: float | None
    refined_only_gold_fraction: float | None
    coarse_metrics: ForegroundMetricResult
    refined_metrics: ForegroundMetricResult
    profile: StageDisagreementProfile


@dataclass(frozen=True)
class DisagreementCalibrationGate:
    """Fail-closed decision for reducing human review."""

    status: CalibrationGateStatus
    triad_count: int
    actionable_count: int
    supported_count: int
    contradicted_count: int
    categories: tuple[str, ...]
    review_reduction_allowed: bool


def _binary_mask(content: bytes, width: int, height: int) -> Image.Image:
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            if (
                image.format != "PNG"
                or image.mode != "L"
                or image.size
                != (
                    width,
                    height,
                )
            ):
                raise DomainRuleError("foreground_calibration_mask_invalid")
            return image.point(lambda value: 255 if value >= 128 else 0, mode="L")
    except DomainRuleError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise DomainRuleError("foreground_calibration_mask_invalid") from None


def _active_fraction(mask: Image.Image, region: Image.Image) -> float | None:
    region_count = region.histogram()[255]
    if region_count == 0:
        return None
    return ImageChops.multiply(mask, region).histogram()[255] / region_count


def _directional_verdict(
    classification: DisagreementClassification,
    coarse_only_gold_fraction: float | None,
) -> CalibrationVerdict:
    if coarse_only_gold_fraction is None:
        return "inconclusive"
    if classification == "coarse_only_detached_dominant":
        if coarse_only_gold_fraction <= 0.20:
            return "supported"
        if coarse_only_gold_fraction >= 0.50:
            return "contradicted"
    elif classification == "coarse_only_boundary_dominant":
        if coarse_only_gold_fraction >= 0.50:
            return "supported"
        if coarse_only_gold_fraction <= 0.20:
            return "contradicted"
    return "inconclusive"


def calibrate_disagreement_sample(
    *,
    sample_id: str,
    category: str,
    coarse_mask_png: bytes,
    refined_mask_png: bytes,
    gold_mask_png: bytes,
    width: int,
    height: int,
) -> DisagreementCalibrationSample:
    """Check one directional hint against gold without treating either model as truth."""
    if not sample_id.strip() or not category.strip() or width <= 0 or height <= 0:
        raise DomainRuleError("foreground_calibration_sample_invalid")
    coarse = _binary_mask(coarse_mask_png, width, height)
    refined = _binary_mask(refined_mask_png, width, height)
    gold = _binary_mask(gold_mask_png, width, height)
    coarse_only = ImageChops.subtract(coarse, refined)
    refined_only = ImageChops.subtract(refined, coarse)
    coarse_only_gold_fraction = _active_fraction(gold, coarse_only)
    refined_only_gold_fraction = _active_fraction(gold, refined_only)
    profile = profile_extraction_stage_disagreement(
        coarse_mask_png,
        refined_mask_png,
        width,
        height,
    )
    return DisagreementCalibrationSample(
        sample_id=sample_id,
        category=category,
        classification=profile.classification,
        verdict=_directional_verdict(
            profile.classification,
            coarse_only_gold_fraction,
        ),
        coarse_only_gold_fraction=coarse_only_gold_fraction,
        refined_only_gold_fraction=refined_only_gold_fraction,
        coarse_metrics=score_foreground_mask(
            coarse_mask_png,
            gold_mask_png,
            width,
            height,
        ),
        refined_metrics=score_foreground_mask(
            refined_mask_png,
            gold_mask_png,
            width,
            height,
        ),
        profile=profile,
    )


def decide_disagreement_calibration_gate(
    samples: tuple[DisagreementCalibrationSample, ...],
    *,
    required_categories: tuple[str, ...] = (
        "bracelet",
        "earring",
        "necklace",
        "pendant",
        "ring",
    ),
    minimum_triads: int = 50,
    minimum_actionable: int = 15,
    minimum_supported_ratio: float = 0.90,
) -> DisagreementCalibrationGate:
    """Allow review reduction only after broad, contradiction-free gold evidence."""
    if (
        minimum_triads <= 0
        or minimum_actionable <= 0
        or not 0 < minimum_supported_ratio <= 1
    ):
        raise ValueError("invalid foreground disagreement calibration policy")
    categories = tuple(sorted({sample.category for sample in samples}))
    actionable = tuple(sample for sample in samples if sample.verdict != "inconclusive")
    supported_count = sum(sample.verdict == "supported" for sample in actionable)
    contradicted_count = sum(sample.verdict == "contradicted" for sample in actionable)
    if len(samples) < minimum_triads or len(actionable) < minimum_actionable:
        status: CalibrationGateStatus = "insufficient_triads"
    elif not set(required_categories).issubset(categories):
        status = "insufficient_category_coverage"
    elif (
        contradicted_count
        or supported_count / len(actionable) < minimum_supported_ratio
    ):
        status = "directional_hints_failed"
    else:
        status = "eligible_for_review_reduction"
    return DisagreementCalibrationGate(
        status=status,
        triad_count=len(samples),
        actionable_count=len(actionable),
        supported_count=supported_count,
        contradicted_count=contradicted_count,
        categories=categories,
        review_reduction_allowed=status == "eligible_for_review_reduction",
    )
