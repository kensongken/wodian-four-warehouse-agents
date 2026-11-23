"""Fail-closed quality gates before identity-locked paid generation."""

from collections.abc import Mapping, Sequence
from typing import Any

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.style_rules import MINIMUM_STYLE_SAMPLES

STYLE_SIGNAL_FIELDS = (
    "tone",
    "palette",
    "lighting",
    "composition",
    "backgrounds",
    "people_and_props",
    "material_treatment",
)
MINIMUM_STYLE_SIGNAL_FIELDS = 3


def require_paid_style_readiness(
    *,
    style_content: Mapping[str, Any],
    active_sample_count: int,
    slot_rules: Mapping[str, Any],
) -> None:
    """Reject a paid run until evidence, observations, and placement rules exist."""

    if active_sample_count < MINIMUM_STYLE_SAMPLES:
        raise DomainRuleError("identity_locked_style_samples_insufficient")

    observations = _resolved_observations(style_content)
    meaningful_fields = sum(
        _has_meaningful_value(observations.get(field))
        for field in STYLE_SIGNAL_FIELDS
    )
    if meaningful_fields < MINIMUM_STYLE_SIGNAL_FIELDS:
        raise DomainRuleError("identity_locked_style_observations_incomplete")

    if not _has_meaningful_slot_rules(slot_rules):
        raise DomainRuleError("identity_locked_slot_rules_incomplete")


def _resolved_observations(style_content: Mapping[str, Any]) -> dict[str, Any]:
    resolved = {field: style_content.get(field) for field in STYLE_SIGNAL_FIELDS}
    overrides = style_content.get("overrides")
    if isinstance(overrides, Mapping):
        for field in STYLE_SIGNAL_FIELDS:
            if _has_meaningful_value(overrides.get(field)):
                resolved[field] = overrides[field]
    return resolved


def _has_meaningful_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_has_meaningful_value(item) for item in value)
    return value is not None and value is not False


def _has_meaningful_slot_rules(slot_rules: Mapping[str, Any]) -> bool:
    # Aspect ratio alone describes the file shape, not a photographic layout.
    return any(
        key != "aspect_ratio" and _has_meaningful_value(value)
        for key, value in slot_rules.items()
    )
