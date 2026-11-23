"""Pure, provider-neutral comparison for one product identity observation."""

from dataclasses import dataclass
from typing import Literal

from opc_style.domain.product_identity import CoreAccessory, ProductIdentityDraft

IdentityGateDecision = Literal["pass", "needs_review", "block"]

IDENTITY_GATE_POLICY_VERSION = "product-identity-gate-v1"

_STRONG_FIELDS = (
    ("category", "category_mismatch"),
    ("component_arrangement", "component_arrangement_mismatch"),
    ("overall_form", "overall_form_mismatch"),
    ("visible_component_count", "visible_component_count_mismatch"),
)


@dataclass(frozen=True)
class IdentityGateResult:
    """One deterministic decision derived from a card and a photo observation."""

    decision: IdentityGateDecision
    reason_codes: tuple[str, ...]
    policy_version: str = IDENTITY_GATE_POLICY_VERSION


def compare_product_identity(
    card: ProductIdentityDraft,
    observation: ProductIdentityDraft,
) -> IdentityGateResult:
    """Compare visible structural facts without granting the observer authority."""
    block_reasons: set[str] = set()
    review_reasons: set[str] = set()

    visibility_limited = _visibility_limited(observation)
    if visibility_limited:
        review_reasons.add("visibility_limited")

    for field_name, mismatch_reason in _STRONG_FIELDS:
        card_field = getattr(card, field_name)
        observed_field = getattr(observation, field_name)
        if card_field.certainty == "unclear" or observed_field.certainty == "unclear":
            review_reasons.add("strong_field_unclear")
            continue
        if card_field.value != observed_field.value:
            if field_name == "component_arrangement" and _equivalent_arrangement(
                card, observation
            ):
                continue
            if field_name == "visible_component_count" and visibility_limited:
                continue
            block_reasons.add(mismatch_reason)

    _compare_accessories(
        card,
        observation,
        visibility_limited=visibility_limited,
        block_reasons=block_reasons,
        review_reasons=review_reasons,
    )

    if block_reasons:
        return IdentityGateResult("block", tuple(sorted(block_reasons)))
    if review_reasons:
        return IdentityGateResult("needs_review", tuple(sorted(review_reasons)))
    return IdentityGateResult("pass", ())


def _equivalent_arrangement(
    card: ProductIdentityDraft, observation: ProductIdentityDraft
) -> bool:
    """Treat overlapping labels as equivalent only for a visible beaded loop."""
    pair = {card.component_arrangement.value, observation.component_arrangement.value}
    if pair != {"single_strand", "continuous_chain"}:
        return False
    if card.category.value != "bracelet" or observation.category.value != "bracelet":
        return False
    if card.overall_form.value != "closed_loop" or observation.overall_form.value != "closed_loop":
        return False
    for draft in (card, observation):
        if draft.component_shapes.certainty != "observed":
            return False
        shapes = draft.component_shapes.value
        if not isinstance(shapes, tuple) or "round_bead" not in shapes:
            return False
    return True


def _visibility_limited(observation: ProductIdentityDraft) -> bool:
    limits = observation.visibility_limits
    if limits.certainty == "unclear":
        return True
    return bool(limits.value)


def _compare_accessories(
    card: ProductIdentityDraft,
    observation: ProductIdentityDraft,
    *,
    visibility_limited: bool,
    block_reasons: set[str],
    review_reasons: set[str],
) -> None:
    expected = card.core_accessories
    observed = observation.core_accessories
    if expected.certainty == "unclear" or observed.certainty == "unclear":
        review_reasons.add("strong_field_unclear")
        return

    expected_by_kind = _accessories_by_kind(expected.value)
    observed_by_kind = _accessories_by_kind(observed.value)
    expected_kinds = set(expected_by_kind)
    observed_kinds = set(observed_by_kind)

    if observed_kinds - expected_kinds:
        block_reasons.add("core_accessory_extra")
    if expected_kinds - observed_kinds:
        if visibility_limited:
            review_reasons.add("visibility_limited")
        else:
            block_reasons.add("core_accessory_missing")

    shared_kinds = expected_kinds & observed_kinds
    if any(expected_by_kind[kind] != observed_by_kind[kind] for kind in shared_kinds):
        if visibility_limited:
            review_reasons.add("visibility_limited")
        else:
            block_reasons.add("core_accessory_count_mismatch")


def _accessories_by_kind(value: object) -> dict[str, int]:
    if not isinstance(value, tuple):
        return {}
    return {
        accessory.kind: accessory.visible_count
        for accessory in value
        if isinstance(accessory, CoreAccessory)
    }
