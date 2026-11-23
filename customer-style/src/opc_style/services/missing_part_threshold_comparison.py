"""Offline-only comparison of missing-part preflight threshold candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from opc_style.domain.missing_part_completion import (
    MIN_MASK_CONTEXT_CHROMA_MEAN,
    MIN_MASK_CONTEXT_CHROMA_STDDEV,
    MIN_MASK_CONTEXT_EDGE_MEAN,
    MIN_MASK_CONTEXT_STRONG_CHROMA_FRACTION,
    MIN_MASK_CONTEXT_STRONG_EDGE_FRACTION,
)

HumanLabel = Literal["confirmed_structure", "confirmed_background"]


@dataclass(frozen=True)
class ThresholdObservation:
    human_label: HumanLabel
    stored_system_state: str
    context_edge_mean: float
    strong_edge_fraction: float
    context_chroma_mean: float
    strong_chroma_fraction: float
    context_chroma_stddev: float


def compare_preflight_thresholds(
    observations: tuple[ThresholdObservation, ...],
    *,
    target_per_label: int = 20,
) -> dict[str, object]:
    """Compare bounded candidates without changing live runtime thresholds."""

    structure_count = sum(
        item.human_label == "confirmed_structure" for item in observations
    )
    background_count = sum(
        item.human_label == "confirmed_background" for item in observations
    )
    ready = structure_count >= target_per_label and background_count >= target_per_label
    current = _evaluate(observations, 1.0)
    drift = sum(
        _predict(item, 1.0) != item.stored_system_state for item in observations
    )
    result: dict[str, object] = {
        "sample_count": len(observations),
        "confirmed_structure": structure_count,
        "confirmed_background": background_count,
        "target_per_label": target_per_label,
        "structure_remaining": max(0, target_per_label - structure_count),
        "background_remaining": max(0, target_per_label - background_count),
        "ready_for_offline_comparison": ready,
        "stored_state_drift_count": drift,
        "current": current,
        "candidate": None,
        "improves_on_current": False,
        "recommended_for_controlled_pilot": False,
        "automatic_activation": False,
        "live_thresholds_changed": False,
        "provider_called": False,
    }
    if not ready:
        result["blockers"] = [
            blocker
            for blocker, remaining in (
                ("confirmed_structure_samples_missing", result["structure_remaining"]),
                ("confirmed_background_samples_missing", result["background_remaining"]),
            )
            if remaining
        ]
        return result

    candidates = [
        {**_evaluate(observations, scale), "threshold_scale": scale}
        for scale in (0.75, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2, 1.3)
    ]
    candidates.sort(
        key=lambda item: (
            int(item["false_pass"]) * 5 + int(item["false_block"]),
            int(item["false_pass"]),
            abs(float(item["threshold_scale"]) - 1.0),
            -float(item["threshold_scale"]),
        )
    )
    best = candidates[0]
    current_score = int(current["false_pass"]) * 5 + int(current["false_block"])
    best_score = int(best["false_pass"]) * 5 + int(best["false_block"])
    improves = (
        best_score < current_score
        and int(best["false_pass"]) <= int(current["false_pass"])
    )
    result.update(
        {
            "candidate": best,
            "improves_on_current": improves,
            "recommended_for_controlled_pilot": (
                improves and int(best["false_pass"]) == 0 and drift == 0
            ),
            "blockers": [] if improves else ["no_verified_improvement"],
        }
    )
    return result


def observation_from_queue_item(item: dict[str, object]) -> ThresholdObservation:
    label = item.get("human_label")
    if label not in {"confirmed_structure", "confirmed_background"}:
        raise ValueError("missing_part_comparison_label_invalid")
    return ThresholdObservation(
        human_label=label,
        stored_system_state=str(item["system_state"]),
        context_edge_mean=cast(float, item["context_edge_mean"]),
        strong_edge_fraction=cast(float, item["strong_edge_fraction"]),
        context_chroma_mean=cast(float, item["context_chroma_mean"]),
        strong_chroma_fraction=cast(float, item["strong_chroma_fraction"]),
        context_chroma_stddev=cast(float, item["context_chroma_stddev"]),
    )


def _predict(item: ThresholdObservation, scale: float) -> str:
    anchored = (
        item.context_edge_mean >= MIN_MASK_CONTEXT_EDGE_MEAN * scale
        and item.strong_edge_fraction
        >= MIN_MASK_CONTEXT_STRONG_EDGE_FRACTION * scale
    ) or (
        item.context_chroma_stddev >= MIN_MASK_CONTEXT_CHROMA_STDDEV * scale
        and item.context_chroma_mean >= MIN_MASK_CONTEXT_CHROMA_MEAN * scale
        and item.strong_chroma_fraction
        >= MIN_MASK_CONTEXT_STRONG_CHROMA_FRACTION * scale
    )
    return "anchored_to_visible_structure" if anchored else "blocked_unanchored"


def _evaluate(
    observations: tuple[ThresholdObservation, ...], scale: float
) -> dict[str, int | float]:
    true_structure = false_pass = false_block = true_background = 0
    for item in observations:
        predicted = _predict(item, scale)
        if item.human_label == "confirmed_structure":
            if predicted == "anchored_to_visible_structure":
                true_structure += 1
            else:
                false_block += 1
        elif predicted == "blocked_unanchored":
            true_background += 1
        else:
            false_pass += 1
    return {
        "threshold_scale": scale,
        "true_structure": true_structure,
        "true_background": true_background,
        "false_pass": false_pass,
        "false_block": false_block,
        "total_errors": false_pass + false_block,
    }
