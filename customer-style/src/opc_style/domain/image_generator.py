"""Provider-neutral in-memory generation contracts and a synthetic local adapter."""

import json
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from typing import Literal, Protocol
from uuid import UUID

from PIL import Image, ImageDraw

DETERMINISTIC_LOCAL_ADAPTER_VERSION = "deterministic-local-image-v1"
SYNTHETIC_TEST_GENERATION_MODE = "synthetic_test_only"
SYNTHETIC_CANDIDATE_COUNT = 3
SYNTHETIC_IMAGE_SIZE = (512, 512)
MIN_REAL_STYLE_REFERENCES = 2
MAX_STYLE_REFERENCES = 4


class ImageGenerationError(Exception):
    """A stable fail-closed image generation error."""

    def __init__(self, code: str, *, outbound_attempts: int = 0) -> None:
        self.code = code
        self.outbound_attempts = outbound_attempts
        super().__init__(code)


@dataclass(frozen=True)
class GenerationRequest:
    """Immutable facts supplied to a replaceable image generator."""

    output_type: str
    product_ids: tuple[str, ...]
    asset_ids: tuple[UUID, ...]
    style_card_version_id: UUID
    main_style_card_version_id: UUID | None
    scene_style_id: UUID
    output_ratio: str
    sources: tuple["GenerationSource", ...] = ()
    slot_key: str = "legacy"
    slot_rules: dict[str, object] = field(default_factory=dict)
    style_observations: dict[str, object] = field(default_factory=dict)
    product_identity_card_version_id: UUID | None = None
    product_identity_card_sha256: str | None = None
    product_identity_card: dict[str, object] | None = None
    wear_presentation: dict[str, object] | None = None
    style_reference_manifest_sha256: str | None = None
    candidate_count: int = SYNTHETIC_CANDIDATE_COUNT


@dataclass(frozen=True)
class GenerationSource:
    """Store-verified source bytes with no provider-visible local path."""

    asset_id: UUID
    product_id: str | None
    content: bytes
    content_sha256: str
    mime_type: str
    role: Literal["product_identity", "style_reference"] = "product_identity"


@dataclass(frozen=True)
class GeneratedCandidate:
    """Encoded candidate bytes with no filesystem publication authority."""

    content: bytes
    content_sha256: str
    mime_type: str
    width: int
    height: int
    metadata: dict[str, object]


class ImageGenerator(Protocol):
    """Replaceable provider contract that never grants filesystem authority."""

    adapter_version: str
    generation_mode: str
    publishable: bool

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        """Return a complete in-memory candidate batch."""
        ...


class DeterministicLocalImageGenerator:
    """Create visibly synthetic JPEGs without source media or external calls."""

    adapter_version = DETERMINISTIC_LOCAL_ADAPTER_VERSION
    generation_mode = SYNTHETIC_TEST_GENERATION_MODE
    publishable = False

    def __init__(
        self,
        *,
        authenticity_fixture: Literal["pass", "subject_incomplete"] = "pass",
    ) -> None:
        self._authenticity_fixture = authenticity_fixture

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        """Return exactly three deterministic, in-memory, non-publishable artifacts."""
        request_fingerprint = _request_fingerprint(request)
        if not 1 <= request.candidate_count <= SYNTHETIC_CANDIDATE_COUNT:
            raise ImageGenerationError("generation_candidate_count_invalid")
        return [
            self._candidate(request, request_fingerprint, sequence_number)
            for sequence_number in range(1, request.candidate_count + 1)
        ]

    def _candidate(
        self,
        request: GenerationRequest,
        request_fingerprint: str,
        sequence_number: int,
    ) -> GeneratedCandidate:
        seed = sha256(f"{request_fingerprint}:{sequence_number}".encode()).digest()
        background = (seed[0] // 2, seed[1] // 2, seed[2] // 2)
        accent = (128 + seed[3] // 2, 128 + seed[4] // 2, 128 + seed[5] // 2)
        image = Image.new("RGB", SYNTHETIC_IMAGE_SIZE, background)
        draw = ImageDraw.Draw(image)
        draw.rectangle((32, 32, 479, 479), outline=accent, width=12)
        for offset in range(96, 416, 80):
            draw.line((64, offset, 447, offset), fill=accent, width=4)
        draw.text((150, 224), "SYNTHETIC TEST ONLY", fill=(255, 255, 255))
        draw.text((204, 248), f"CANDIDATE {sequence_number}", fill=accent)

        output = BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=85,
            optimize=False,
            progressive=False,
            subsampling=0,
        )
        content = output.getvalue()
        authenticity_fixture = {
            "subject_complete": self._authenticity_fixture != "subject_incomplete",
            "product_shape_preserved": True,
            "material_preserved": True,
            "component_count_preserved": True,
            "color_shift_within_fixture_tolerance": True,
            "unexpected_artifacts": [],
        }
        metadata: dict[str, object] = {
            "adapter_version": self.adapter_version,
            "generation_mode": self.generation_mode,
            "synthetic_test_only": True,
            "publishable": False,
            "artifact_label": "synthetic local test image; not production AI output",
            "sequence_number": sequence_number,
            "request_fingerprint": request_fingerprint,
            "output_type": request.output_type,
            "output_ratio": request.output_ratio,
            "source_product_ids": list(request.product_ids),
            "source_asset_ids": [str(asset_id) for asset_id in request.asset_ids],
            "style_card_version_id": str(request.style_card_version_id),
            "main_style_card_version_id": (
                str(request.main_style_card_version_id)
                if request.main_style_card_version_id
                else None
            ),
            "scene_style_id": str(request.scene_style_id),
            # Explicit synthetic fixture facts for the deterministic local gate.
            # These markers are test inputs, not claims from a vision model.
            "local_authenticity_fixture": authenticity_fixture,
        }
        return GeneratedCandidate(
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/jpeg",
            width=SYNTHETIC_IMAGE_SIZE[0],
            height=SYNTHETIC_IMAGE_SIZE[1],
            metadata=metadata,
        )


def _request_fingerprint(request: GenerationRequest) -> str:
    canonical = json.dumps(
        {
            "output_type": request.output_type,
            "product_ids": list(request.product_ids),
            "asset_ids": [str(asset_id) for asset_id in request.asset_ids],
            "style_card_version_id": str(request.style_card_version_id),
            "main_style_card_version_id": str(request.main_style_card_version_id),
            "scene_style_id": str(request.scene_style_id),
            "output_ratio": request.output_ratio,
            "slot_key": request.slot_key,
            "slot_rules": request.slot_rules,
            "style_observations": request.style_observations,
            "product_identity_card_version_id": str(
                request.product_identity_card_version_id
            ),
            "product_identity_card_sha256": request.product_identity_card_sha256,
            "wear_presentation": request.wear_presentation,
            "style_reference_manifest_sha256": (
                request.style_reference_manifest_sha256
            ),
            "candidate_count": request.candidate_count,
            "source_roles_and_hashes": [
                (source.role, str(source.asset_id), source.content_sha256)
                for source in request.sources
            ],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()
