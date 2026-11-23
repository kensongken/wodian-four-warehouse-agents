"""Fail-closed contract for separating a display carrier from real occlusion."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from opc_style.domain.errors import DomainRuleError

CarrierType = Literal[
    "hand",
    "glove",
    "mannequin",
    "tray",
    "stand",
    "person",
    "other",
]
OcclusionState = Literal["present", "absent", "unclear"]
OccludedPart = Literal[
    "bead",
    "chain",
    "ring_band",
    "connector",
    "clasp",
    "pendant",
    "product_edge",
    "other",
]
ReviewAction = Literal["continue", "manual_review", "block"]

SCHEMA_VERSION = "actual-occlusion-assessment-v1"
CARRIER_TYPES = frozenset(
    {"hand", "glove", "mannequin", "tray", "stand", "person", "other"}
)
OCCLUDED_PARTS = frozenset(
    {
        "bead",
        "chain",
        "ring_band",
        "connector",
        "clasp",
        "pendant",
        "product_edge",
        "other",
    }
)
OCCLUSION_STATES = frozenset({"present", "absent", "unclear"})
_FIELDS = frozenset(
    {
        "schema_version",
        "carrier_present",
        "carrier_types",
        "actual_occlusion",
        "occluded_parts",
        "evidence_note",
        "human_confirmed",
    }
)
_INVALID = "actual_occlusion_assessment_invalid"


@dataclass(frozen=True)
class ActualOcclusionAssessment:
    """One two-axis assessment and the safe action derived from it."""

    carrier_present: bool
    carrier_types: tuple[CarrierType, ...]
    actual_occlusion: OcclusionState
    occluded_parts: tuple[OccludedPart, ...]
    evidence_note: str
    human_confirmed: bool
    review_action: ReviewAction


def normalize_actual_occlusion_assessment(
    payload: Mapping[str, object],
) -> ActualOcclusionAssessment:
    """Validate evidence without treating carrier presence as occlusion."""
    if not isinstance(payload, Mapping) or set(payload) != _FIELDS:
        raise DomainRuleError(_INVALID)
    if payload["schema_version"] != SCHEMA_VERSION:
        raise DomainRuleError(_INVALID)

    carrier_present = payload["carrier_present"]
    human_confirmed = payload["human_confirmed"]
    if type(carrier_present) is not bool or type(human_confirmed) is not bool:
        raise DomainRuleError(_INVALID)

    carrier_types = cast(
        tuple[CarrierType, ...],
        _normalize_values(payload["carrier_types"], CARRIER_TYPES),
    )
    if carrier_present != bool(carrier_types):
        raise DomainRuleError(_INVALID)

    raw_actual_occlusion = payload["actual_occlusion"]
    if raw_actual_occlusion not in OCCLUSION_STATES:
        raise DomainRuleError(_INVALID)
    actual_occlusion = cast(OcclusionState, raw_actual_occlusion)
    occluded_parts = cast(
        tuple[OccludedPart, ...],
        _normalize_values(payload["occluded_parts"], OCCLUDED_PARTS),
    )
    if actual_occlusion == "present" and not occluded_parts:
        raise DomainRuleError(_INVALID)
    if actual_occlusion != "present" and occluded_parts:
        raise DomainRuleError(_INVALID)
    if human_confirmed and actual_occlusion == "unclear":
        raise DomainRuleError(_INVALID)

    evidence_note = payload["evidence_note"]
    if not isinstance(evidence_note, str):
        raise DomainRuleError(_INVALID)
    evidence_note = " ".join(evidence_note.split())
    if not evidence_note or len(evidence_note) > 240:
        raise DomainRuleError(_INVALID)

    if not human_confirmed:
        action: ReviewAction = "manual_review"
    elif actual_occlusion == "present":
        action = "block"
    else:
        action = "continue"

    return ActualOcclusionAssessment(
        carrier_present=carrier_present,
        carrier_types=carrier_types,
        actual_occlusion=actual_occlusion,
        occluded_parts=occluded_parts,
        evidence_note=evidence_note,
        human_confirmed=human_confirmed,
        review_action=action,
    )


def _normalize_values(value: object, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DomainRuleError(_INVALID)
    if len(value) != len(set(value)) or any(item not in allowed for item in value):
        raise DomainRuleError(_INVALID)
    return tuple(value)
