"""One-attempt OpenAI image-edit adapter for bounded missing-part candidates."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from io import BytesIO

import httpx
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.missing_part_completion import (
    MissingPartCandidate,
    MissingPartCompletionRequest,
    compose_missing_part_candidate,
    require_missing_part_evidence,
    validate_missing_part_request,
)

OPENAI_IMAGE_EDIT_URL = "https://api.openai.com/v1/images/edits"
OPENAI_MISSING_PART_MODEL = "gpt-image-2.5-sunburst-2026-09-08"
OPENAI_MISSING_PART_MODEL_VERSION = (
    "openai-gpt-image-2.5-sunburst-2026-09-08-missing-part-v1"
)
OPENAI_MISSING_PART_QUALITY = "high"
OPENAI_MISSING_PART_SELECTION_REASON = (
    "precision_masked_jewelry_edit_with_1024px_context"
)
PROVIDER_CANVAS_SIZE = 1024
MIN_CONTEXT_EDGE = 512

PART_CONSTRAINTS = {
    "ear_hook": "Continue one ear-hook wire only; preserve its thickness, curve and attachment point.",
    "fine_chain": "Continue only the chain links implied by both visible ends; add no clasp, pendant or branch.",
    "clasp": "Join only the two visible terminals with one clasp in the evidenced orientation.",
    "bead_edge": "Restore only the missing boundary of the existing bead; do not change bead count or order.",
    "pendant_edge": "Continue only the existing pendant outline; add no engraving, motif or stone.",
    "setting_edge": "Continue the evidenced metal setting or spacer; do not turn it into a bead or stone.",
}


@dataclass(frozen=True)
class _ProviderCanvas:
    source_png: bytes
    mask_png: bytes
    crop_box: tuple[int, int, int, int]


class OpenAIMissingPartCompleter:
    """Generate pixels for one declared region; never grants publish authority."""

    model_version = OPENAI_MISSING_PART_MODEL_VERSION

    def __init__(
        self,
        *,
        api_key: str,
        transport: httpx.Client,
        model: str = OPENAI_MISSING_PART_MODEL,
        timeout_seconds: int = 300,
    ) -> None:
        if not api_key.strip() or model != OPENAI_MISSING_PART_MODEL:
            raise DomainRuleError("missing_part_provider_disabled")
        self._api_key = api_key
        self._transport = transport
        self._model = model
        self._timeout_seconds = timeout_seconds

    def complete(
        self,
        request: MissingPartCompletionRequest,
        *,
        learning_lessons: tuple[str, ...] = (),
    ) -> MissingPartCandidate:
        validated = validate_missing_part_request(request)
        require_missing_part_evidence(request)
        prompt = _controlled_prompt(request, validated.missing_ratio, learning_lessons)
        canvas = _provider_canvas(request)
        provider_images = [
            (
                "image[]",
                ("product-context.png", canvas.source_png, "image/png"),
            )
        ]
        if request.evidence_kind == "clean_same_product_reference":
            assert request.reference_content is not None
            assert request.reference_mime_type is not None
            provider_images.append(
                (
                    "image[]",
                    (
                        "same-product-reference",
                        request.reference_content,
                        request.reference_mime_type,
                    ),
                )
            )
        if request.evidence_kind == "same_product_multiview_references":
            for index, (content, mime_type) in enumerate(
                zip(
                    request.reference_contents,
                    request.reference_mime_types,
                    strict=True,
                ),
                start=1,
            ):
                provider_images.append(
                    (
                        "image[]",
                        (f"same-product-view-{index}", content, mime_type),
                    )
                )
        try:
            response = self._transport.post(
                OPENAI_IMAGE_EDIT_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                data={
                    "model": self._model,
                    "prompt": prompt,
                    "n": "1",
                    "size": f"{PROVIDER_CANVAS_SIZE}x{PROVIDER_CANVAS_SIZE}",
                    "quality": OPENAI_MISSING_PART_QUALITY,
                    "output_format": "png",
                },
                files=[
                    *provider_images,
                    ("mask", ("missing-region.png", canvas.mask_png, "image/png")),
                ],
                timeout=self._timeout_seconds,
                follow_redirects=False,
            )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
        ) as error:
            raise DomainRuleError("missing_part_provider_unavailable") from error
        if response.status_code in {401, 403}:
            raise DomainRuleError("missing_part_provider_auth_failed")
        if response.status_code == 429:
            raise DomainRuleError("missing_part_provider_rate_limited")
        if not response.is_success:
            raise DomainRuleError("missing_part_provider_rejected")
        generated = _restore_generated_canvas(
            request, _response_image(response), canvas.crop_box
        )
        return compose_missing_part_candidate(
            request,
            generated_content=generated,
            model_version=self.model_version,
        )


def _controlled_prompt(
    request: MissingPartCompletionRequest,
    missing_ratio: float,
    lessons: tuple[str, ...],
) -> str:
    bounded_lessons = [lesson.strip()[:300] for lesson in lessons[:5] if lesson.strip()]
    return "\n".join(
        (
            "AI-simulated missing region. Edit only the transparent mask area.",
            "The supplied product photo is the only product identity authority.",
            (
                "A second image is a human-confirmed clean photo of the exact same "
                "physical product, and the requested missing part is confirmed visible "
                "and unobstructed in it. Use it only as structural evidence for the "
                "masked part; preserve the first image's angle, scale, lighting and identity."
                if request.evidence_kind == "clean_same_product_reference"
                else (
                    "The additional images are human-confirmed views of the exact "
                    "same physical product. Together they clearly evidence the requested "
                    "missing part. Triangulate only the shared visible structure; do not "
                    "invent details that no view supports. Preserve the first image's "
                    "angle, scale, lighting and identity."
                    if request.evidence_kind == "same_product_multiview_references"
                    else "No separate same-product reference image is supplied."
                )
            ),
            f"Product category: {request.category}.",
            f"Missing part kind: {request.part_kind}.",
            "Part-specific constraint: " + PART_CONSTRAINTS[request.part_kind],
            "Material tags: " + ", ".join(sorted(set(request.material_tags))),
            f"Masked image ratio: {missing_ratio:.6f}.",
            (
                "Continue visible geometry, lighting, reflections and material only "
                "from adjacent pixels. Do not add stones, beads, engravings, hallmarks, "
                "charms, clasps, or decorative details that are not evidenced by the "
                "source. Do not alter any unmasked pixel."
            ),
            "Human-confirmed prior lessons: "
            + (
                json.dumps(bounded_lessons, ensure_ascii=True)
                if bounded_lessons
                else "none"
            ),
            "Return one conservative completion candidate. This is a model guess and requires human review.",
        )
    )


def _provider_canvas(request: MissingPartCompletionRequest) -> _ProviderCanvas:
    with Image.open(BytesIO(request.source_content)) as opened:
        source = opened.convert("RGB")
    with Image.open(BytesIO(request.missing_mask_png)) as opened:
        binary = opened.convert("L").point(
            lambda value: 255 if value >= 128 else 0, mode="L"
        )
    bbox = binary.getbbox()
    if bbox is None:
        raise DomainRuleError("missing_part_region_empty")
    crop_box = _context_crop_box(source.size, bbox)
    source_crop = source.crop(crop_box).resize(
        (PROVIDER_CANVAS_SIZE, PROVIDER_CANVAS_SIZE), Image.Resampling.LANCZOS
    )
    mask_crop = binary.crop(crop_box).resize(
        (PROVIDER_CANVAS_SIZE, PROVIDER_CANVAS_SIZE), Image.Resampling.NEAREST
    )
    rgba = Image.new(
        "RGBA", (PROVIDER_CANVAS_SIZE, PROVIDER_CANVAS_SIZE), (255, 255, 255, 255)
    )
    rgba.putalpha(mask_crop.point(lambda value: 0 if value else 255, mode="L"))
    source_output = BytesIO()
    source_crop.save(source_output, format="PNG", optimize=False)
    output = BytesIO()
    rgba.save(output, format="PNG", optimize=False)
    return _ProviderCanvas(
        source_png=source_output.getvalue(),
        mask_png=output.getvalue(),
        crop_box=crop_box,
    )


def _context_crop_box(
    source_size: tuple[int, int], mask_box: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    width, height = source_size
    left, top, right, bottom = mask_box
    edge = min(
        max(MIN_CONTEXT_EDGE, (right - left) * 8, (bottom - top) * 8), width, height
    )
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    crop_left = min(max(center_x - edge // 2, 0), width - edge)
    crop_top = min(max(center_y - edge // 2, 0), height - edge)
    return crop_left, crop_top, crop_left + edge, crop_top + edge


def _restore_generated_canvas(
    request: MissingPartCompletionRequest,
    generated_content: bytes,
    crop_box: tuple[int, int, int, int],
) -> bytes:
    with Image.open(BytesIO(request.source_content)) as opened:
        source = opened.convert("RGB")
    try:
        with Image.open(BytesIO(generated_content)) as opened:
            opened.load()
            generated = opened.convert("RGB")
    except (Image.DecompressionBombError, OSError, ValueError):
        raise DomainRuleError("missing_part_provider_response_invalid") from None
    crop_size = (crop_box[2] - crop_box[0], crop_box[3] - crop_box[1])
    generated = generated.resize(crop_size, Image.Resampling.LANCZOS)
    source.paste(generated, crop_box)
    output = BytesIO()
    source.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _response_image(response: httpx.Response) -> bytes:
    try:
        payload = response.json()
        encoded = payload["data"][0]["b64_json"]
        if not isinstance(encoded, str):
            raise TypeError
        content = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(content)) as opened:
            opened.verify()
        return content
    except (KeyError, IndexError, TypeError, ValueError, OSError):
        raise DomainRuleError("missing_part_provider_response_invalid") from None
