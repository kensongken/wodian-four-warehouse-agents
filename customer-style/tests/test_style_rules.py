import pytest

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.style_rules import assess_sample_count, enforce_scene_limit


def test_style_thresholds_report_the_correct_readiness_at_boundaries() -> None:
    assert assess_sample_count(0).samples_needed == 10
    assert assess_sample_count(9).samples_needed == 1
    assert assess_sample_count(10).stability == "initial"
    assert assess_sample_count(19).stability == "initial"
    assert assess_sample_count(20).stability == "stable"


def test_scene_limit_rejects_a_sixth_active_scene() -> None:
    enforce_scene_limit(4)

    with pytest.raises(DomainRuleError, match="scene_limit_reached") as error:
        enforce_scene_limit(5)

    assert error.value.code == "scene_limit_reached"
