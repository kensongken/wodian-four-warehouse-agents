"""Immutable contracts for locally extracted, pixel-preserved product foregrounds."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from string import hexdigits
from typing import Literal, Protocol
from uuid import UUID

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError

_MASK_INVALID = "foreground_mask_invalid"
_SOURCE_INCOMPLETE = "foreground_source_incomplete"
_TRANSFORM_INVALID = "foreground_transform_invalid"


@dataclass(frozen=True)
class ForegroundPromptHints:
    """Optional normalized prompts for local candidate extractors."""

    box: tuple[float, float, float, float] | None = None
    positive_points: tuple[tuple[float, float], ...] = ()
    negative_points: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class ForegroundExtractionRequest:
    """Verified source bytes and immutable ownership facts supplied to an extractor."""

    customer_id: UUID
    asset_id: UUID
    product_id: str
    source_content: bytes
    source_content_sha256: str
    mime_type: str
    width: int
    height: int
    prompt_hints: ForegroundPromptHints | None = None


@dataclass(frozen=True)
class ForegroundStageReceipt:
    """Digest-bound evidence for one local foreground pipeline stage."""

    role: Literal["coarse_segmenter", "alpha_refiner"]
    artifact_sha256: str
    output_sha256: str
    parameters_sha256: str


@dataclass(frozen=True)
class ExtractedForeground:
    """RGBA product pixels and their same-size, single-channel placement mask."""

    foreground_png: bytes
    foreground_sha256: str
    mask_png: bytes
    mask_sha256: str
    width: int
    height: int
    extractor_version: str
    coarse_mask_png: bytes | None = None
    coarse_mask_sha256: str | None = None
    stage_receipts: tuple[ForegroundStageReceipt, ...] = ()
    pipeline_manifest_sha256: str | None = None


@dataclass(frozen=True)
class ForegroundQuality:
    """Recomputed facts used to decide whether the extraction can be reviewed."""

    foreground_pixel_count: int
    total_pixel_count: int
    canvas_width: int
    canvas_height: int
    bounding_box: tuple[int, int, int, int]
    touches_edge: bool


def foreground_source_quality_flags(
    quality: ForegroundQuality,
    *,
    minimum_subject_span_ratio: float = 0.40,
) -> tuple[str, ...]:
    """Flag sources whose extracted subject is too small for a primary-image crop."""
    if not 0 < minimum_subject_span_ratio < 1:
        raise ValueError("minimum_subject_span_ratio must be between zero and one")
    left, top, right, bottom = quality.bounding_box
    width = right - left
    height = bottom - top
    span_ratio = max(
        width / quality.canvas_width,
        height / quality.canvas_height,
    )
    if span_ratio < minimum_subject_span_ratio:
        return ("subject_too_small_for_primary_image",)
    return ()


@dataclass(frozen=True)
class MaskEditMetrics:
    """Server-recomputed facts comparing a base mask with one human edit."""

    added_pixel_count: int
    deleted_pixel_count: int
    result_pixel_count: int
    total_pixel_count: int
    component_count: int
    change_ratio: float
    large_change_warning: bool
    touches_edge: bool


@dataclass(frozen=True)
class EditedForeground:
    """Foreground bytes rebuilt from sealed source RGB and an edited alpha mask."""

    foreground_png: bytes
    foreground_sha256: str
    mask_png: bytes
    mask_sha256: str
    width: int
    height: int
    extractor_version: str = "human-mask-editor-v1"


@dataclass(frozen=True)
class ForegroundTransform:
    """The complete transform allowlist for a pixel-preserving composition."""

    translate_x: int
    translate_y: int
    uniform_scale: Decimal
    rotation_degrees: Decimal


class ProductForegroundExtractor(Protocol):
    """Provider-neutral local foreground extraction boundary."""

    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        """Extract an RGBA foreground and mask without changing source ownership."""
        ...


def _is_sha256(value: str | None) -> bool:
    return bool(
        value
        and len(value) == 64
        and all(character in hexdigits for character in value)
    )


def _validate_pipeline_evidence(result: ExtractedForeground) -> None:
    has_pipeline_evidence = bool(
        result.pipeline_manifest_sha256
        or result.coarse_mask_png
        or result.coarse_mask_sha256
        or result.stage_receipts
        or result.extractor_version.startswith("local-sam2-birefnet-")
    )
    if not has_pipeline_evidence:
        return
    if (
        result.coarse_mask_png is None
        or not _is_sha256(result.coarse_mask_sha256)
        or sha256(result.coarse_mask_png).hexdigest() != result.coarse_mask_sha256
        or not _is_sha256(result.pipeline_manifest_sha256)
        or tuple(receipt.role for receipt in result.stage_receipts)
        != ("coarse_segmenter", "alpha_refiner")
    ):
        raise DomainRuleError("foreground_pipeline_evidence_invalid")
    for receipt in result.stage_receipts:
        if not all(
            _is_sha256(value)
            for value in (
                receipt.artifact_sha256,
                receipt.output_sha256,
                receipt.parameters_sha256,
            )
        ):
            raise DomainRuleError("foreground_pipeline_evidence_invalid")
    if (
        result.stage_receipts[0].output_sha256 != result.coarse_mask_sha256
        or result.stage_receipts[1].output_sha256 != result.mask_sha256
    ):
        raise DomainRuleError("foreground_pipeline_evidence_invalid")


def stage_receipts_sha256(receipts: tuple[ForegroundStageReceipt, ...]) -> str:
    """Hash ordered, path-free stage evidence for persistence and recovery."""
    if tuple(receipt.role for receipt in receipts) != (
        "coarse_segmenter",
        "alpha_refiner",
    ):
        raise DomainRuleError("foreground_pipeline_evidence_invalid")
    payload = [
        {
            "artifact_sha256": receipt.artifact_sha256,
            "output_sha256": receipt.output_sha256,
            "parameters_sha256": receipt.parameters_sha256,
            "role": receipt.role,
        }
        for receipt in receipts
    ]
    if any(
        not _is_sha256(value)
        for item in payload
        for key, value in item.items()
        if key != "role"
    ):
        raise DomainRuleError("foreground_pipeline_evidence_invalid")
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    return sha256(encoded).hexdigest()


def validate_extracted_foreground(result: ExtractedForeground) -> ForegroundQuality:
    """Validate dimensions, hashes, alpha support, and complete canvas containment."""
    if (
        result.width <= 0
        or result.height <= 0
        or not result.extractor_version.strip()
        or sha256(result.foreground_png).hexdigest() != result.foreground_sha256
        or sha256(result.mask_png).hexdigest() != result.mask_sha256
    ):
        raise DomainRuleError(_MASK_INVALID)

    _validate_pipeline_evidence(result)
    try:
        with Image.open(BytesIO(result.foreground_png)) as foreground_image:
            foreground_image.load()
            if foreground_image.format != "PNG" or foreground_image.mode != "RGBA":
                raise DomainRuleError(_MASK_INVALID)
            foreground = foreground_image.copy()
        with Image.open(BytesIO(result.mask_png)) as mask_image:
            mask_image.load()
            if mask_image.format != "PNG" or mask_image.mode != "L":
                raise DomainRuleError(_MASK_INVALID)
            mask = mask_image.copy()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise DomainRuleError(_MASK_INVALID) from error

    declared_size = (result.width, result.height)
    if foreground.size != declared_size or mask.size != declared_size:
        raise DomainRuleError(_MASK_INVALID)

    binary_mask = mask.point(lambda value: 255 if value else 0, mode="L")
    bounding_box = binary_mask.getbbox()
    total_pixel_count = result.width * result.height
    foreground_pixel_count = total_pixel_count - binary_mask.histogram()[0]
    if (
        bounding_box is None
        or foreground_pixel_count == 0
        or foreground_pixel_count == total_pixel_count
    ):
        raise DomainRuleError(_MASK_INVALID)

    alpha = foreground.getchannel("A").point(
        lambda value: 255 if value else 0, mode="L"
    )
    if alpha.tobytes() != binary_mask.tobytes():
        raise DomainRuleError(_MASK_INVALID)

    left, top, right, bottom = bounding_box
    touches_edge = (
        left == 0 or top == 0 or right == result.width or bottom == result.height
    )
    return ForegroundQuality(
        foreground_pixel_count=foreground_pixel_count,
        total_pixel_count=total_pixel_count,
        canvas_width=result.width,
        canvas_height=result.height,
        bounding_box=bounding_box,
        touches_edge=touches_edge,
    )


def _load_mask(mask_png: bytes, width: int, height: int) -> Image.Image:
    if width <= 0 or height <= 0:
        raise DomainRuleError("foreground_edit_invalid")
    try:
        with Image.open(BytesIO(mask_png)) as source:
            source.load()
            if source.format != "PNG" or source.mode != "L":
                raise DomainRuleError("foreground_edit_invalid")
            mask = source.copy()
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise DomainRuleError("foreground_edit_invalid") from None
    if mask.size != (width, height):
        raise DomainRuleError("foreground_edit_invalid")
    return mask.point(lambda value: 255 if value >= 128 else 0, mode="L")


def normalize_edited_mask(mask_png: bytes, width: int, height: int) -> bytes:
    """Return a canonical binary mask or reject unsafe formal edit results."""
    mask = _load_mask(mask_png, width, height)
    histogram = mask.histogram()
    foreground_pixels = width * height - histogram[0]
    if foreground_pixels in {0, width * height}:
        raise DomainRuleError("foreground_edit_invalid")
    bounding_box = mask.getbbox()
    if bounding_box is None:
        raise DomainRuleError("foreground_edit_invalid")
    left, top, right, bottom = bounding_box
    if left == 0 or top == 0 or right == width or bottom == height:
        raise DomainRuleError("foreground_edit_touches_edge")
    output = BytesIO()
    mask.save(output, format="PNG", optimize=False)
    return output.getvalue()


def mask_pixels_sha256(mask_png: bytes, width: int, height: int) -> str:
    """Hash normalized pixels so browsers and Pillow agree across PNG encoders."""
    return sha256(_load_mask(mask_png, width, height).tobytes()).hexdigest()


def _component_count(mask: Image.Image) -> int:
    width, height = mask.size
    active = bytearray(1 if value else 0 for value in mask.get_flattened_data())
    components = 0
    for start in range(width * height):
        if not active[start]:
            continue
        components += 1
        active[start] = 0
        stack = [start]
        while stack:
            current = stack.pop()
            x, y = current % width, current // width
            for neighbor_y in range(max(0, y - 1), min(height, y + 2)):
                for neighbor_x in range(max(0, x - 1), min(width, x + 2)):
                    neighbor = neighbor_y * width + neighbor_x
                    if active[neighbor]:
                        active[neighbor] = 0
                        stack.append(neighbor)
    return components


def compute_mask_edit_metrics(
    base_mask_png: bytes,
    edited_mask_png: bytes,
    width: int,
    height: int,
) -> MaskEditMetrics:
    """Compare canonical masks and reject a no-op edit."""
    base = _load_mask(base_mask_png, width, height)
    edited_bytes = normalize_edited_mask(edited_mask_png, width, height)
    edited = _load_mask(edited_bytes, width, height)
    base_values = base.tobytes()
    edited_values = edited.tobytes()
    if base_values == edited_values:
        raise DomainRuleError("foreground_edit_invalid")
    added = sum(
        1
        for before, after in zip(base_values, edited_values, strict=True)
        if not before and after
    )
    deleted = sum(
        1
        for before, after in zip(base_values, edited_values, strict=True)
        if before and not after
    )
    base_count = sum(1 for value in base_values if value)
    result_count = sum(1 for value in edited_values if value)
    change_ratio = (added + deleted) / max(base_count, 1)
    return MaskEditMetrics(
        added_pixel_count=added,
        deleted_pixel_count=deleted,
        result_pixel_count=result_count,
        total_pixel_count=width * height,
        component_count=_component_count(edited),
        change_ratio=change_ratio,
        large_change_warning=change_ratio > 0.15,
        touches_edge=False,
    )


def rebuild_foreground_from_original(
    source_content: bytes,
    normalized_mask_png: bytes,
    width: int,
    height: int,
) -> EditedForeground:
    """Apply edited alpha to the exact sealed source RGB without repainting it."""
    mask_bytes = normalize_edited_mask(normalized_mask_png, width, height)
    mask = _load_mask(mask_bytes, width, height)
    try:
        with Image.open(BytesIO(source_content)) as source_image:
            source_image.load()
            if source_image.size != (width, height):
                raise DomainRuleError("foreground_edit_invalid")
            foreground = source_image.convert("RGBA")
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise DomainRuleError("foreground_edit_invalid") from None
    foreground.putalpha(mask)
    output = BytesIO()
    foreground.save(output, format="PNG", optimize=False)
    foreground_png = output.getvalue()
    return EditedForeground(
        foreground_png=foreground_png,
        foreground_sha256=sha256(foreground_png).hexdigest(),
        mask_png=mask_bytes,
        mask_sha256=sha256(mask_bytes).hexdigest(),
        width=width,
        height=height,
    )


def mask_edit_manifest_sha256(
    *,
    customer_id: str,
    asset_id: str,
    base_version_id: str,
    source_content_sha256: str,
    base_manifest_sha256: str,
    base_mask_sha256: str,
    result_mask_pixels_sha256: str,
    editor_version: str,
    magic_wand_count: int,
    erase_stroke_count: int,
    restore_stroke_count: int,
) -> str:
    """Bind one client-declared edit to server-verifiable immutable facts."""
    digests = (
        source_content_sha256,
        base_manifest_sha256,
        base_mask_sha256,
        result_mask_pixels_sha256,
    )
    counts = (magic_wand_count, erase_stroke_count, restore_stroke_count)
    if (
        any(len(value) != 64 for value in digests)
        or not customer_id.strip()
        or not asset_id.strip()
        or not base_version_id.strip()
        or editor_version != "human-mask-editor-v1"
        or any(isinstance(value, bool) or value < 0 for value in counts)
    ):
        raise DomainRuleError("foreground_edit_manifest_mismatch")
    payload = {
        "asset_id": asset_id,
        "base_manifest_sha256": base_manifest_sha256,
        "base_mask_sha256": base_mask_sha256,
        "base_version_id": base_version_id,
        "customer_id": customer_id,
        "editor_version": editor_version,
        "erase_stroke_count": erase_stroke_count,
        "magic_wand_count": magic_wand_count,
        "restore_stroke_count": restore_stroke_count,
        "result_mask_pixels_sha256": result_mask_pixels_sha256,
        "source_content_sha256": source_content_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def validate_foreground_transform(transform: ForegroundTransform) -> None:
    """Allow only finite uniform scale, integer translation, and a small rotation."""
    if (
        isinstance(transform.translate_x, bool)
        or not isinstance(transform.translate_x, int)
        or isinstance(transform.translate_y, bool)
        or not isinstance(transform.translate_y, int)
        or not transform.uniform_scale.is_finite()
        or not transform.rotation_degrees.is_finite()
        or not Decimal(0) < transform.uniform_scale <= Decimal(4)
        or not Decimal(-5) <= transform.rotation_degrees <= Decimal(5)
    ):
        raise DomainRuleError(_TRANSFORM_INVALID)


def foreground_manifest_sha256(
    request: ForegroundExtractionRequest,
    result: ExtractedForeground,
) -> str:
    """Hash immutable source and extraction facts into a stable confirmation manifest."""
    if (
        request.width <= 0
        or request.height <= 0
        or not request.product_id.strip()
        or not request.mime_type.strip()
        or sha256(request.source_content).hexdigest() != request.source_content_sha256
    ):
        raise DomainRuleError(_SOURCE_INCOMPLETE)

    validate_extracted_foreground(result)
    return foreground_manifest_evidence_sha256(
        request,
        extractor_version=result.extractor_version,
        foreground_sha256=result.foreground_sha256,
        mask_sha256=result.mask_sha256,
        width=result.width,
        height=result.height,
        pipeline_manifest_sha256=result.pipeline_manifest_sha256,
        stage_evidence_sha256=(
            stage_receipts_sha256(result.stage_receipts)
            if result.stage_receipts
            else None
        ),
    )


def foreground_manifest_evidence_sha256(
    request: ForegroundExtractionRequest,
    *,
    extractor_version: str,
    foreground_sha256: str,
    mask_sha256: str,
    width: int,
    height: int,
    pipeline_manifest_sha256: str | None = None,
    stage_evidence_sha256: str | None = None,
) -> str:
    """Hash persisted foreground facts after byte-level validation by the caller."""
    payload = {
        "asset_id": str(request.asset_id),
        "customer_id": str(request.customer_id),
        "extractor_version": extractor_version,
        "foreground_sha256": foreground_sha256,
        "height": height,
        "mask_sha256": mask_sha256,
        "mime_type": request.mime_type,
        "product_id": request.product_id,
        "source_content_sha256": request.source_content_sha256,
        "source_height": request.height,
        "source_width": request.width,
        "width": width,
    }
    if pipeline_manifest_sha256 is not None or stage_evidence_sha256 is not None:
        if not _is_sha256(pipeline_manifest_sha256) or not _is_sha256(
            stage_evidence_sha256
        ):
            raise DomainRuleError("foreground_pipeline_evidence_invalid")
        payload["pipeline_manifest_sha256"] = pipeline_manifest_sha256
        payload["stage_receipts_sha256"] = stage_evidence_sha256
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
