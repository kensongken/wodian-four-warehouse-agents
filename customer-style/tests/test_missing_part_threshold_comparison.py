from __future__ import annotations

from opc_style.services.missing_part_threshold_comparison import (
    ThresholdObservation,
    compare_preflight_thresholds,
)


def _observation(label: str, edge: float, fraction: float) -> ThresholdObservation:
    assert label in {"confirmed_structure", "confirmed_background"}
    return ThresholdObservation(
        human_label=label,  # type: ignore[arg-type]
        stored_system_state=(
            "anchored_to_visible_structure"
            if edge >= 8 and fraction >= 0.04
            else "blocked_unanchored"
        ),
        context_edge_mean=edge,
        strong_edge_fraction=fraction,
        context_chroma_mean=0,
        strong_chroma_fraction=0,
        context_chroma_stddev=0,
    )


def test_comparison_stays_blocked_until_both_labels_reach_target() -> None:
    result = compare_preflight_thresholds(
        tuple(_observation("confirmed_structure", 9, 0.05) for _ in range(19))
        + tuple(_observation("confirmed_background", 3, 0.01) for _ in range(20))
    )
    assert result["ready_for_offline_comparison"] is False
    assert result["structure_remaining"] == 1
    assert result["candidate"] is None
    assert result["automatic_activation"] is False


def test_comparison_can_recommend_bounded_candidate_without_activation() -> None:
    observations = tuple(
        _observation("confirmed_structure", 7.5, 0.038) for _ in range(20)
    ) + tuple(_observation("confirmed_background", 2, 0.005) for _ in range(20))
    result = compare_preflight_thresholds(observations)
    assert result["ready_for_offline_comparison"] is True
    assert result["current"] == {
        "threshold_scale": 1.0,
        "true_structure": 0,
        "true_background": 20,
        "false_pass": 0,
        "false_block": 20,
        "total_errors": 20,
    }
    candidate = result["candidate"]
    assert isinstance(candidate, dict)
    assert candidate["false_pass"] == 0
    assert candidate["false_block"] == 0
    assert result["recommended_for_controlled_pilot"] is True
    assert result["automatic_activation"] is False
    assert result["live_thresholds_changed"] is False
