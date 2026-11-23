"""Paid generation readiness is evidence-based and fail-closed."""

import pytest

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.generation_readiness import require_paid_style_readiness


def _assert_code(code: str, **overrides: object) -> None:
    values: dict[str, object] = {
        "style_content": {
            "overrides": {
                "palette": ["warm"],
                "lighting": ["soft"],
                "backgrounds": ["plain"],
            }
        },
        "active_sample_count": 10,
        "slot_rules": {"aspect_ratio": "4:5", "camera_angle": "front"},
    }
    values.update(overrides)
    with pytest.raises(DomainRuleError) as raised:
        require_paid_style_readiness(**values)  # type: ignore[arg-type]
    assert raised.value.code == code


def test_paid_readiness_requires_ten_sealed_style_samples() -> None:
    _assert_code("identity_locked_style_samples_insufficient", active_sample_count=9)


def test_paid_readiness_requires_multiple_real_style_signals() -> None:
    _assert_code(
        "identity_locked_style_observations_incomplete",
        style_content={"overrides": {"palette": ["warm"]}},
    )


def test_paid_readiness_requires_layout_rules_beyond_file_ratio() -> None:
    _assert_code(
        "identity_locked_slot_rules_incomplete",
        slot_rules={"aspect_ratio": "4:5"},
    )


def test_paid_readiness_accepts_confirmed_evidence_and_rules() -> None:
    require_paid_style_readiness(
        style_content={
            "overrides": {
                "palette": ["warm"],
                "lighting": ["soft"],
                "backgrounds": ["plain"],
            }
        },
        active_sample_count=10,
        slot_rules={"aspect_ratio": "4:5", "camera_angle": "front"},
    )
