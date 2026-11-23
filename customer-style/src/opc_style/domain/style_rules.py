"""Business rules for deciding whether a customer style is ready."""

from dataclasses import dataclass
from enum import StrEnum

from opc_style.domain.errors import DomainRuleError

MINIMUM_STYLE_SAMPLES = 10
STABLE_STYLE_SAMPLES = 20
MAXIMUM_ACTIVE_SCENES = 5
SCENE_CHANNEL_FIELDS = ("aspect_ratio", "purpose", "usage_rules")


class SceneAspectRatio(StrEnum):
    """Supported output frames for phase-one scene contracts."""

    SQUARE = "1:1"
    LANDSCAPE_PHOTO = "3:2"
    PORTRAIT_PHOTO = "2:3"
    LANDSCAPE_STANDARD = "4:3"
    PORTRAIT_STANDARD = "3:4"
    LANDSCAPE_SOCIAL = "5:4"
    PORTRAIT_SOCIAL = "4:5"
    LANDSCAPE_WIDE = "16:9"
    PORTRAIT_TALL = "9:16"


@dataclass(frozen=True)
class SampleReadiness:
    """The customer-visible readiness state derived from active samples."""

    active_sample_count: int
    samples_needed: int
    stability: str
    can_generate: bool


def assess_sample_count(active_count: int) -> SampleReadiness:
    """Describe whether active authorized samples can support a style card."""
    if active_count < 0:
        raise ValueError("active_count must not be negative")

    samples_needed = max(MINIMUM_STYLE_SAMPLES - active_count, 0)
    if active_count < MINIMUM_STYLE_SAMPLES:
        stability = "insufficient"
    elif active_count < STABLE_STYLE_SAMPLES:
        stability = "initial"
    else:
        stability = "stable"

    return SampleReadiness(
        active_sample_count=active_count,
        samples_needed=samples_needed,
        stability=stability,
        can_generate=active_count >= MINIMUM_STYLE_SAMPLES,
    )


def enforce_scene_limit(active_scene_count: int) -> None:
    """Reject creation when a customer already has five active scene styles."""
    if active_scene_count < 0:
        raise ValueError("active_scene_count must not be negative")
    if active_scene_count >= MAXIMUM_ACTIVE_SCENES:
        raise DomainRuleError("scene_limit_reached")
