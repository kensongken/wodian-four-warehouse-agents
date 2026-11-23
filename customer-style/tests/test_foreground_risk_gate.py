from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import RiskSignals
from opc_style.services.foreground_risk_gate import (
    RiskThresholds,
    compare_extraction_stages,
    evaluate_shadow_risk,
    profile_extraction_stage_disagreement,
)


def _signals(**overrides: object) -> RiskSignals:
    values: dict[str, object] = {
        "coarse_refined_iou": 0.96,
        "area_delta_ratio": 0.02,
        "touches_edge": False,
        "component_count": 2,
        "fine_structure_ratio": 0.98,
        "high_risk_components": (),
        "evidence_complete": True,
    }
    values.update(overrides)
    return RiskSignals(**values)  # type: ignore[arg-type]


def test_complete_low_risk_evidence_is_only_technically_auto_eligible() -> None:
    assert evaluate_shadow_risk(_signals(), RiskThresholds()) == "auto_eligible"


@pytest.mark.parametrize(
    "signals",
    [
        _signals(evidence_complete=False),
        _signals(coarse_refined_iou=None),
        _signals(area_delta_ratio=None),
    ],
)
def test_missing_candidate_or_incomplete_evidence_is_blocked(
    signals: RiskSignals,
) -> None:
    assert evaluate_shadow_risk(signals, RiskThresholds()) == "blocked"


@pytest.mark.parametrize(
    "signals",
    [
        _signals(coarse_refined_iou=0.70),
        _signals(area_delta_ratio=0.30),
        _signals(touches_edge=True),
        _signals(component_count=99),
        _signals(fine_structure_ratio=0.50),
        _signals(high_risk_components=("clasp",)),
        _signals(high_risk_components=("pendant",)),
        _signals(high_risk_components=("transparent",)),
    ],
)
def test_disagreement_or_jewelry_component_risk_requires_human_review(
    signals: RiskSignals,
) -> None:
    assert evaluate_shadow_risk(signals, RiskThresholds()) == "human_review_required"


def test_thresholds_require_a_sealed_valid_configuration() -> None:
    with pytest.raises(DomainRuleError) as error:
        evaluate_shadow_risk(_signals(), RiskThresholds(config_sha256="bad"))
    assert error.value.code == "foreground_risk_thresholds_invalid"


def test_calibrated_bad_cohort_never_becomes_auto_eligible() -> None:
    bad_samples = (
        _signals(coarse_refined_iou=0.79),
        _signals(area_delta_ratio=0.16),
        _signals(fine_structure_ratio=0.89),
        _signals(high_risk_components=("pendant",)),
        _signals(evidence_complete=False),
    )

    assert all(
        evaluate_shadow_risk(sample, RiskThresholds()) != "auto_eligible"
        for sample in bad_samples
    )


def _mask(values: list[list[int]]) -> bytes:
    image = Image.new("L", (len(values[0]), len(values)))
    image.putdata([value for row in values for value in row])
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_disagreement_profile_separates_detached_coarse_overreach_candidate() -> None:
    coarse = _mask(
        [
            [255, 255, 0, 0, 0, 0, 0, 0, 0],
            [255, 255, 0, 0, 0, 0, 255, 255, 0],
            [255, 255, 0, 0, 0, 0, 255, 255, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0],
        ]
    )
    refined = _mask(
        [
            [0, 0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 255, 255, 0],
            [0, 0, 0, 0, 0, 0, 255, 255, 0],
            [0, 0, 0, 0, 0, 0, 0, 0, 0],
        ]
    )

    profile = profile_extraction_stage_disagreement(coarse, refined, 9, 4)

    assert profile.classification == "coarse_only_detached_dominant"
    assert profile.coarse_only_ratio == pytest.approx(0.6)
    assert profile.refined_only_ratio == 0.0
    assert profile.coarse_only_detached_ratio == 1.0


def test_disagreement_profile_keeps_boundary_removal_as_detail_risk_candidate() -> None:
    coarse = _mask(
        [
            [0, 0, 0, 0, 0, 0],
            [0, 255, 255, 255, 255, 0],
            [0, 255, 255, 255, 255, 0],
            [0, 0, 0, 0, 0, 0],
        ]
    )
    refined = _mask(
        [
            [0, 0, 0, 0, 0, 0],
            [0, 255, 255, 0, 0, 0],
            [0, 255, 255, 0, 0, 0],
            [0, 0, 0, 0, 0, 0],
        ]
    )

    profile = profile_extraction_stage_disagreement(coarse, refined, 6, 4)

    assert profile.classification == "coarse_only_boundary_dominant"
    assert profile.coarse_only_detached_ratio == pytest.approx(0.5)


def test_high_risk_component_requires_review_even_when_masks_agree() -> None:
    mask = _mask(
        [[0] * 6, [0, 255, 255, 255, 255, 0], [0, 255, 255, 255, 255, 0], [0] * 6]
    )

    signals = compare_extraction_stages(
        mask,
        mask,
        6,
        4,
        high_risk_components=("pendant",),
        evidence_complete=True,
    )

    assert signals.coarse_refined_iou == 1.0
    assert evaluate_shadow_risk(signals, RiskThresholds()) == "human_review_required"


def test_missing_stage_receipt_is_blocked() -> None:
    mask = _mask(
        [[0] * 6, [0, 255, 255, 255, 255, 0], [0, 255, 255, 255, 255, 0], [0] * 6]
    )

    signals = compare_extraction_stages(
        mask,
        mask,
        6,
        4,
        high_risk_components=(),
        evidence_complete=False,
    )

    assert evaluate_shadow_risk(signals, RiskThresholds()) == "blocked"


def test_large_support_delta_requires_review() -> None:
    coarse = _mask(
        [[0] * 6, [0, 255, 255, 255, 255, 0], [0, 255, 255, 255, 255, 0], [0] * 6]
    )
    refined = _mask([[0] * 6, [0, 255, 0, 0, 0, 0], [0, 255, 0, 0, 0, 0], [0] * 6])

    signals = compare_extraction_stages(
        coarse,
        refined,
        6,
        4,
        high_risk_components=(),
        evidence_complete=True,
    )

    assert signals.area_delta_ratio == 0.75
    assert evaluate_shadow_risk(signals, RiskThresholds()) == "human_review_required"


def test_soft_alpha_edges_do_not_count_as_background_components() -> None:
    coarse = _mask(
        [[0] * 7, [0, 255, 255, 255, 255, 0, 0], [0, 255, 255, 255, 255, 0, 0], [0] * 7]
    )
    refined = _mask(
        [
            [0, 40, 0, 0, 0, 0, 40],
            [0, 255, 255, 255, 255, 0, 0],
            [0, 255, 255, 255, 255, 0, 0],
            [0] * 7,
        ]
    )

    signals = compare_extraction_stages(
        coarse,
        refined,
        7,
        4,
        high_risk_components=(),
        evidence_complete=True,
    )

    assert signals.component_count == 1
    assert signals.coarse_refined_iou == 1.0
