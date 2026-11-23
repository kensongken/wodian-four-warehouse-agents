"""Deterministic comparison of a confirmed product card and a new-photo observation."""

from collections import OrderedDict

from opc_style.domain.product_identity import normalize_identity_draft
from opc_style.domain.product_identity_gate import compare_product_identity


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "category": {"value": "bracelet", "certainty": "observed"},
        "primary_colors": {
            "value": ["red", "gold_tone"],
            "certainty": "observed",
        },
        "visible_surface_appearance": {
            "value": ["glossy"],
            "certainty": "observed",
        },
        "component_shapes": {
            "value": ["round_bead"],
            "certainty": "observed",
        },
        "visible_component_count": {"value": 18, "certainty": "observed"},
        "component_arrangement": {
            "value": "single_strand",
            "certainty": "observed",
        },
        "core_accessories": {
            "value": [{"kind": "clasp", "visible_count": 1}],
            "certainty": "observed",
        },
        "overall_form": {"value": "closed_loop", "certainty": "observed"},
        "relative_proportions": {
            "value": "主体珠大小接近，扣头明显更小",
            "certainty": "observed",
        },
        "distinguishing_marks": {
            "value": "一颗主体珠有深色斑点",
            "certainty": "observed",
        },
        "visibility_limits": {"value": [], "certainty": "observed"},
    }
    payload.update(overrides)
    return payload


def _compare(
    card_payload: dict[str, object], observation_payload: dict[str, object]
):
    return compare_product_identity(
        normalize_identity_draft(card_payload),
        normalize_identity_draft(observation_payload),
    )


def test_same_product_observation_passes_across_a_different_photo() -> None:
    result = _compare(_payload(), _payload())

    assert result.decision == "pass"
    assert result.reason_codes == ()
    assert result.policy_version == "product-identity-gate-v1"


def test_single_strand_and_continuous_chain_are_equivalent_for_one_beaded_loop() -> None:
    observation = _payload(
        component_arrangement={
            "value": "continuous_chain",
            "certainty": "observed",
        }
    )

    result = _compare(_payload(), observation)

    assert result.decision == "pass"
    assert result.reason_codes == ()


def test_hidden_clasp_requires_review() -> None:
    observation = _payload(
        core_accessories={"value": [], "certainty": "observed"},
        visibility_limits={"value": ["clasp_hidden"], "certainty": "observed"},
    )

    result = _compare(_payload(), observation)

    assert result.decision == "needs_review"
    assert result.reason_codes == ("visibility_limited",)


def test_unclear_strong_field_requires_review() -> None:
    observation = _payload(category={"value": None, "certainty": "unclear"})

    result = _compare(_payload(), observation)

    assert result.decision == "needs_review"
    assert result.reason_codes == ("strong_field_unclear",)


def test_extra_pendant_blocks() -> None:
    observation = _payload(
        core_accessories={
            "value": [
                {"kind": "clasp", "visible_count": 1},
                {"kind": "pendant", "visible_count": 1},
            ],
            "certainty": "observed",
        }
    )

    result = _compare(_payload(), observation)

    assert result.decision == "block"
    assert result.reason_codes == ("core_accessory_extra",)


def test_missing_clasp_blocks_when_visibility_is_clear() -> None:
    observation = _payload(
        core_accessories={"value": [], "certainty": "observed"}
    )

    result = _compare(_payload(), observation)

    assert result.decision == "block"
    assert result.reason_codes == ("core_accessory_missing",)


def test_core_accessory_count_mismatch_blocks() -> None:
    observation = _payload(
        core_accessories={
            "value": [{"kind": "clasp", "visible_count": 2}],
            "certainty": "observed",
        }
    )

    result = _compare(_payload(), observation)

    assert result.decision == "block"
    assert result.reason_codes == ("core_accessory_count_mismatch",)


def test_category_form_arrangement_and_count_mismatches_block() -> None:
    observation = _payload(
        category={"value": "necklace", "certainty": "observed"},
        visible_component_count={"value": 17, "certainty": "observed"},
        component_arrangement={
            "value": "continuous_chain",
            "certainty": "observed",
        },
        overall_form={"value": "open_chain", "certainty": "observed"},
    )

    result = _compare(_payload(), observation)

    assert result.decision == "block"
    assert result.reason_codes == (
        "category_mismatch",
        "component_arrangement_mismatch",
        "overall_form_mismatch",
        "visible_component_count_mismatch",
    )


def test_comparison_is_independent_of_input_json_key_order() -> None:
    ordered = _payload()
    reversed_order = OrderedDict(reversed(tuple(ordered.items())))

    result = _compare(ordered, dict(reversed_order))

    assert result.decision == "pass"
    assert result.reason_codes == ()
