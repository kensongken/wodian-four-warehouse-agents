"""Background-only generation contracts for identity-locked compositing."""

import json
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any, Protocol

from PIL import Image, ImageDraw

BACKGROUND_WIDTH = 1024
BACKGROUND_HEIGHT = 1280
BACKGROUND_COUNT = 3
BACKGROUND_RATIO = "4:5"


class BackgroundGenerationError(RuntimeError):
    """Stable, user-safe background generation failure."""

    def __init__(self, code: str, *, outbound_attempts: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.outbound_attempts = outbound_attempts


@dataclass(frozen=True)
class BackgroundGenerationRequest:
    """Photography directions only; intentionally cannot carry image bytes."""

    style_observations: dict[str, Any]
    slot_rules: dict[str, Any]
    output_ratio: str
    candidate_count: int = BACKGROUND_COUNT


@dataclass(frozen=True)
class GeneratedBackground:
    """One empty background candidate."""

    content: bytes
    content_sha256: str
    mime_type: str
    width: int
    height: int
    sequence_number: int
    metadata: dict[str, Any]
    synthetic_test_only: bool
    publishable: bool


@dataclass(frozen=True)
class BackgroundSafetyResult:
    """Three-state safety result; None means no evidence and fails closed."""

    safe: bool | None
    reasons: tuple[str, ...]
    checker_version: str


class BackgroundGenerator(Protocol):
    """Generate empty scenes without receiving product or reference media."""

    def generate(
        self, request: BackgroundGenerationRequest
    ) -> list[GeneratedBackground]: ...


class BackgroundSafetyChecker(Protocol):
    """Reject backgrounds that contain products or other contaminants."""

    def check(self, background: GeneratedBackground) -> BackgroundSafetyResult: ...


def controlled_background_prompt(request: BackgroundGenerationRequest) -> str:
    """Build a bounded prompt that explicitly requires an empty scene."""

    directions = json.dumps(
        {
            "style_observations": request.style_observations,
            "slot_rules": request.slot_rules,
            "output_ratio": request.output_ratio,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "\n".join(
        (
            "Generate an empty product-photography background scene.",
            "No jewelry. No product. No person. No hand.",
            "No text. No logo. No packaging.",
            "Reserve the central lower 70 percent width and 60 percent height for later local compositing.",
            "Keep that reserved zone empty, uncluttered, and fully visible.",
            "Use a believable flat support surface and soft directional light so a local contact shadow can blend naturally.",
            "Do not place large props, seams, or hard highlights across the reserved zone.",
            f"Photography directions: {directions}",
        )
    )


class DeterministicLocalBackgroundGenerator:
    """Create three deterministic, synthetic empty scenes for local acceptance."""

    adapter_version = "deterministic-empty-background-v1"
    synthetic_test_only = True

    def generate(
        self, request: BackgroundGenerationRequest
    ) -> list[GeneratedBackground]:
        _validate_request(request)
        palettes = (
            ((243, 238, 230), (220, 207, 191)),
            ((230, 236, 233), (195, 210, 204)),
            ((239, 232, 235), (211, 195, 201)),
        )
        return [
            self._make_background(sequence_number, colors)
            for sequence_number, colors in enumerate(palettes, start=1)
        ]

    def _make_background(
        self,
        sequence_number: int,
        colors: tuple[tuple[int, int, int], tuple[int, int, int]],
    ) -> GeneratedBackground:
        image = Image.new("RGB", (BACKGROUND_WIDTH, BACKGROUND_HEIGHT), colors[0])
        draw = ImageDraw.Draw(image)
        horizon = 920 + sequence_number * 18
        draw.rectangle((0, horizon, BACKGROUND_WIDTH, BACKGROUND_HEIGHT), fill=colors[1])
        draw.ellipse(
            (190, horizon - 42, 834, horizon + 54),
            fill=tuple(max(0, channel - 8) for channel in colors[1]),
        )
        output = BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=False, progressive=False)
        content = output.getvalue()
        return GeneratedBackground(
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/jpeg",
            width=BACKGROUND_WIDTH,
            height=BACKGROUND_HEIGHT,
            sequence_number=sequence_number,
            metadata={
                "adapter_version": self.adapter_version,
                "background_safety_fixture": "safe",
            },
            synthetic_test_only=True,
            publishable=False,
        )


class DeterministicBackgroundSafetyChecker:
    """Fixture checker for deterministic local tests, never production evidence."""

    checker_version = "deterministic-background-safety-v1"

    def check(self, background: GeneratedBackground) -> BackgroundSafetyResult:
        if not background.synthetic_test_only:
            return BackgroundSafetyResult(
                False, ("fixture_checker_forbidden_for_real_background",),
                self.checker_version,
            )
        fixture = background.metadata.get("background_safety_fixture")
        if fixture == "safe":
            return BackgroundSafetyResult(True, (), self.checker_version)
        if fixture == "jewelry_detected":
            return BackgroundSafetyResult(
                False, ("background_contains_jewelry",), self.checker_version
            )
        return BackgroundSafetyResult(
            None, ("background_check_unavailable",), self.checker_version
        )


def _validate_request(request: BackgroundGenerationRequest) -> None:
    if request.output_ratio != BACKGROUND_RATIO:
        raise BackgroundGenerationError("background_ratio_unsupported")
    if request.candidate_count != BACKGROUND_COUNT:
        raise BackgroundGenerationError("background_candidate_count_invalid")
