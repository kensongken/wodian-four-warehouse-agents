"""Replaceable style-analysis contract and a local deterministic test adapter."""

import re
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Protocol, cast
from uuid import UUID

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError

DETERMINISTIC_LOCAL_ANALYZER = "deterministic-local-v1"
SYNTHETIC_METADATA_TEST_MODE = "synthetic_metadata_test_only"
STYLE_FIELD_MAX_ITEMS = 20
STYLE_FIELD_ENTRY_MAX_LENGTH = 200
STYLE_ANALYSIS_LIST_FIELDS = (
    "tone",
    "palette",
    "lighting",
    "composition",
    "backgrounds",
    "people_and_props",
    "material_treatment",
    "typography",
    "forbidden_elements",
)


@dataclass(frozen=True)
class SampleDescriptor:
    """Authorized sample identifiers plus synthetic, non-media test metadata."""

    sample_id: UUID
    photo_id: str
    synthetic_metadata: dict[str, str]


@dataclass(frozen=True)
class StyleAnalysis:
    """Provider-neutral fields required by a customer-editable style card."""

    tone: list[str]
    palette: list[str]
    lighting: list[str]
    composition: list[str]
    backgrounds: list[str]
    people_and_props: list[str]
    material_treatment: list[str]
    typography: list[str]
    forbidden_elements: list[str]
    evidence_photo_ids: list[str]


class StyleAnalyzer(Protocol):
    """Boundary that lets a future analyzer replace the local test adapter."""

    @property
    def label(self) -> str:
        """Return the auditable analyzer name and version."""
        ...

    @property
    def analysis_mode(self) -> str:
        """Return a truthful label for how this adapter produced its result."""
        ...

    def analyze(self, samples: list[SampleDescriptor]) -> StyleAnalysis:
        """Analyze customer-scoped authorized sample descriptors."""
        ...


class DeterministicLocalStyleAnalyzer:
    """Local test-only adapter; it does not inspect media or call production AI."""

    @property
    def label(self) -> str:
        return DETERMINISTIC_LOCAL_ANALYZER

    @property
    def analysis_mode(self) -> str:
        return SYNTHETIC_METADATA_TEST_MODE

    def analyze(self, samples: list[SampleDescriptor]) -> StyleAnalysis:
        """Return repeatable fixture values from synthetic metadata descriptors."""
        if any(
            sample.synthetic_metadata.get("analysis_mode")
            != SYNTHETIC_METADATA_TEST_MODE
            for sample in samples
        ):
            raise ValueError("deterministic_analyzer_requires_synthetic_metadata")

        return StyleAnalysis(
            tone=["自然", "克制"],
            palette=["暖白", "低饱和"],
            lighting=["柔和漫射光"],
            composition=["主体居中", "保留呼吸感"],
            backgrounds=["简洁中性背景"],
            people_and_props=["少量自然道具"],
            material_treatment=["保留真实纹理与光泽"],
            typography=["简洁无衬线排版"],
            forbidden_elements=["强烈滤镜", "改变商品事实"],
            evidence_photo_ids=sorted(sample.photo_id for sample in samples),
        )


# Controlled vocabulary prevents visual descriptions from becoming product facts.
VISUAL_OBSERVATIONS: dict[str, tuple[str, ...]] = {
    "background": ("plain", "textured", "gradient", "environment", "unknown"),
    "lighting": ("soft", "hard", "diffuse", "directional", "unknown"),
    "palette": ("red", "green", "blue", "neutral", "dark", "light", "warm", "cool"),
    "composition": ("centered", "offset", "negative_space", "flat_lay", "unknown"),
    "angle": ("front", "side", "overhead", "oblique", "unknown"),
    "crop": ("close", "full", "partial", "unknown"),
    "props": ("none", "present", "unknown"),
    "aspect_ratio": ("square", "landscape", "portrait"),
    "presentation": ("isolated", "worn", "displayed", "unknown"),
}
VISUAL_SLOTS = ("hero", "detail", "context")
VISUAL_NAMES = ("visual group",) + tuple(
    f"{color} {aspect}"
    for color in VISUAL_OBSERVATIONS["palette"]
    for aspect in VISUAL_OBSERVATIONS["aspect_ratio"]
)


@dataclass(frozen=True)
class VisualStyleSample:
    """Exact in-memory bytes selected by the authorization service."""

    sample_id: UUID
    content: bytes
    content_sha256: str
    mime_type: str


@dataclass(frozen=True)
class StyleGroupProposal:
    """Non-authoritative visual suggestions, linked to supplied evidence only."""

    proposal_key: str
    suggested_name: str
    evidence_sample_ids: tuple[UUID, ...]
    observations: dict[str, tuple[str, ...]]
    suggested_slots: tuple[str, ...]


class VisualStyleAnalyzer(Protocol):
    analyzer_version: str

    def analyze(
        self, samples: tuple[VisualStyleSample, ...]
    ) -> tuple[StyleGroupProposal, ...]:
        """Return evidence-linked proposals without persisting them."""
        ...


def decode_visual_sample(sample: VisualStyleSample) -> Image.Image:
    """Validate exact bytes and bounded decoded pixels before either adapter uses them."""
    try:
        if (
            not sample.content
            or len(sample.content) > 10 * 1024 * 1024
            or sha256(sample.content).hexdigest() != sample.content_sha256
        ):
            raise ValueError("invalid_content")
        with Image.open(BytesIO(sample.content)) as source:
            expected = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
            if (
                expected.get(source.format or "") != sample.mime_type
                or source.width * source.height > 16 * 1024 * 1024
                or getattr(source, "n_frames", 1) != 1
            ):
                raise ValueError("invalid_image")
            source.verify()
        with Image.open(BytesIO(sample.content)) as source:
            return source.convert("RGB")
    except (
        ValueError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as error:
        raise DomainRuleError("style_visual_analysis_unavailable") from error


def validate_visual_proposals(
    groups: tuple[StyleGroupProposal, ...], sample_ids: frozenset[UUID]
) -> tuple[StyleGroupProposal, ...]:
    """Enforce bounded semantics even for custom or compromised analyzers."""
    invalid = "style_visual_analysis_invalid_response"
    if not isinstance(groups, tuple) or not 1 <= len(groups) <= 20:
        raise DomainRuleError(invalid)
    keys: set[str] = set()
    for group in groups:
        if not isinstance(group, StyleGroupProposal):
            raise DomainRuleError(invalid)
        if (
            not isinstance(group.proposal_key, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", group.proposal_key)
            or group.proposal_key in keys
            or group.suggested_name not in VISUAL_NAMES
            or not isinstance(group.evidence_sample_ids, tuple)
            or not group.evidence_sample_ids
            or len(group.evidence_sample_ids) > len(sample_ids)
            or any(not isinstance(value, UUID) for value in group.evidence_sample_ids)
            or len(set(group.evidence_sample_ids)) != len(group.evidence_sample_ids)
            or not set(group.evidence_sample_ids) <= sample_ids
            or not isinstance(group.observations, dict)
            or not group.observations
            or not set(group.observations) <= VISUAL_OBSERVATIONS.keys()
        ):
            raise DomainRuleError(invalid)
        keys.add(group.proposal_key)
        for field, values in group.observations.items():
            if (
                not isinstance(values, tuple)
                or not 1 <= len(values) <= 8
                or any(value not in VISUAL_OBSERVATIONS[field] for value in values)
            ):
                raise DomainRuleError(invalid)
        if (
            not isinstance(group.suggested_slots, tuple)
            or not 1 <= len(group.suggested_slots) <= 3
            or any(slot not in VISUAL_SLOTS for slot in group.suggested_slots)
        ):
            raise DomainRuleError(invalid)
    return groups


class DeterministicPixelStyleAnalyzer:
    """Dimension/color fixture analysis; never production visual learning."""

    analyzer_version = "deterministic-pixels-v1"
    analysis_mode = "synthetic_test_only"

    def analyze(
        self, samples: tuple[VisualStyleSample, ...]
    ) -> tuple[StyleGroupProposal, ...]:
        buckets: dict[tuple[str, str], list[UUID]] = {}
        for sample in samples:
            with decode_visual_sample(sample) as image:
                aspect = (
                    "square"
                    if image.width == image.height
                    else ("landscape" if image.width > image.height else "portrait")
                )
                # Count coarse RGB buckets from actual pixels, with stable tie-breaking.
                image.thumbnail((64, 64))
                pixels = image.load()
                assert pixels is not None
                counts = Counter(
                    _color_bucket(cast(tuple[int, int, int], pixels[x, y]))
                    for x in range(image.width)
                    for y in range(image.height)
                )
                color = min(counts, key=lambda key: (-counts[key], key))
                buckets.setdefault((color, aspect), []).append(sample.sample_id)
        groups = tuple(
            StyleGroupProposal(
                proposal_key=f"{color}-{aspect}",
                suggested_name=f"{color} {aspect}",
                evidence_sample_ids=tuple(sorted(ids, key=str)),
                observations={"palette": (color,), "aspect_ratio": (aspect,)},
                suggested_slots=("hero",),
            )
            for (color, aspect), ids in sorted(buckets.items())
        )
        return validate_visual_proposals(
            groups, frozenset(sample.sample_id for sample in samples)
        )


def _color_bucket(pixel: tuple[int, int, int]) -> str:
    red, green, blue = pixel
    if max(pixel) < 48:
        return "dark"
    if min(pixel) > 208:
        return "light"
    if max(pixel) - min(pixel) < 32:
        return "neutral"
    return ("red", "green", "blue")[max(range(3), key=lambda i: (red, green, blue)[i])]
