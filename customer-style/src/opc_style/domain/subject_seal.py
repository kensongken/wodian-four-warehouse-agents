"""Provider-neutral product-subject sealing contracts."""

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Protocol, cast

from PIL import Image, ImageOps, UnidentifiedImageError

SUPPORTED_SUBJECT_MIME_TYPES = {"image/jpeg", "image/png"}
DETERMINISTIC_SUBJECT_ADAPTER_VERSION = "deterministic-local-subject-v2"
HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION = "human-reviewed-subject-mask-v1"
CORE_ALPHA_THRESHOLD = 255
MIN_REVIEW_RETAINED_ALPHA_RATIO = 0.60
MIN_SUBJECT_EDGE_MARGIN_RATIO = 0.01
PRODUCT_SUBJECT_CANVAS = (768, 768)


class SubjectSealError(Exception):
    """A stable fail-closed error at the subject-segmentation boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SubjectSegmentationRequest:
    """Verified source bytes and server-owned target geometry."""

    content: bytes
    content_sha256: str
    mime_type: str
    target_canvas: tuple[int, int]


@dataclass(frozen=True)
class SealedSubject:
    """One immutable normalized subject plus its protected core mask."""

    rgba_png: bytes
    rgba_sha256: str
    core_mask_png: bytes
    core_mask_sha256: str
    width: int
    height: int
    core_mask_pixel_count: int
    feather_width_px: int
    adapter_version: str
    synthetic_test_only: bool
    publishable: bool = False


class SubjectSegmenter(Protocol):
    """Replaceable foreground extraction boundary."""

    adapter_version: str
    synthetic_test_only: bool

    def seal(self, request: SubjectSegmentationRequest) -> SealedSubject:
        """Return a sealed subject or fail without a whole-image fallback."""
        ...


class DeterministicLocalSubjectSegmenter:
    """Use explicit alpha, or the full image, for zero-network test fixtures."""

    adapter_version = DETERMINISTIC_SUBJECT_ADAPTER_VERSION
    synthetic_test_only = True

    def seal(self, request: SubjectSegmentationRequest) -> SealedSubject:
        source = _fit_to_canvas(_decode_source(request), request.target_canvas)
        if "A" in source.getbands():
            mask = source.getchannel("A")
        else:
            mask = Image.new("L", source.size, 255)
        return _seal_decoded_subject(
            source,
            mask,
            adapter_version=self.adapter_version,
            synthetic_test_only=self.synthetic_test_only,
        )


def seal_subject_with_mask(
    request: SubjectSegmentationRequest,
    mask_png: bytes,
    *,
    adapter_version: str,
    synthetic_test_only: bool,
) -> SealedSubject:
    """Seal verified source bytes with an independently produced alpha mask."""
    source = _decode_source(request)
    mask = _decode_mask(mask_png)
    if mask.size != source.size:
        raise SubjectSealError("subject_mask_dimensions_mismatch")
    fitted_size = _fitted_size(source.size, request.target_canvas)
    if fitted_size != source.size:
        source = source.resize(fitted_size, Image.Resampling.LANCZOS)
        mask = mask.resize(fitted_size, Image.Resampling.LANCZOS)
    return _seal_decoded_subject(
        source,
        mask,
        adapter_version=adapter_version,
        synthetic_test_only=synthetic_test_only,
    )


def seal_normalized_subject(
    request: SubjectSegmentationRequest,
    normalized_source_png: bytes,
    mask_png: bytes,
    *,
    adapter_version: str,
    synthetic_test_only: bool,
) -> SealedSubject:
    """Seal a trusted adapter's cropped source while retaining original lineage."""
    _decode_source(request)
    source = _decode_image(normalized_source_png, "subject_normalized_source_invalid")
    if (
        source.width > request.target_canvas[0]
        or source.height > request.target_canvas[1]
    ):
        raise SubjectSealError("subject_exceeds_target_canvas")
    mask = _decode_mask(mask_png)
    if mask.size != source.size:
        raise SubjectSealError("subject_mask_dimensions_mismatch")
    return _seal_decoded_subject(
        source,
        mask,
        adapter_version=adapter_version,
        synthetic_test_only=synthetic_test_only,
    )


def review_sealed_subject_mask(
    normalized_source_png: bytes,
    reviewed_mask_png: bytes,
    *,
    synthetic_test_only: bool,
    manual_selection: bool = False,
) -> SealedSubject:
    """Apply a human-reviewed erase-only mask to an existing sealed subject.

    The reviewer may remove pixels that the automatic extractor selected, but may
    never reveal pixels outside the automatic mask. This keeps the correction
    reusable without allowing the browser to invent or replace product pixels.
    """
    source = _decode_image(
        normalized_source_png, "subject_normalized_source_invalid"
    ).convert("RGBA")
    reviewed_mask = _decode_review_mask(reviewed_mask_png)
    if reviewed_mask.size != source.size:
        raise SubjectSealError("subject_mask_dimensions_mismatch")
    automatic_alpha = source.getchannel("A")
    automatic_values = tuple(cast(Sequence[int], automatic_alpha.get_flattened_data()))
    reviewed_values = tuple(cast(Sequence[int], reviewed_mask.get_flattened_data()))
    if any(
        reviewed > automatic
        for automatic, reviewed in zip(automatic_values, reviewed_values, strict=True)
    ):
        raise SubjectSealError("subject_review_mask_may_only_erase")
    automatic_selected = sum(value > 0 for value in automatic_values)
    reviewed_selected = sum(value > 0 for value in reviewed_values)
    if automatic_selected == 0 or reviewed_selected / automatic_selected < (
        0 if manual_selection else MIN_REVIEW_RETAINED_ALPHA_RATIO
    ):
        raise SubjectSealError("subject_source_not_cleanly_extractable")
    _require_clean_subject_geometry(reviewed_mask)
    return _seal_decoded_subject(
        source,
        reviewed_mask,
        adapter_version=HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION,
        synthetic_test_only=synthetic_test_only,
    )


def validate_sealed_subject_for_generation(rgba_png: bytes) -> None:
    """Recheck persisted reviewed artifacts so stale weak masks cannot bypass gates."""

    source = _decode_image(rgba_png, "subject_normalized_source_invalid").convert(
        "RGBA"
    )
    _require_clean_subject_geometry(source.getchannel("A"))


def _require_clean_subject_geometry(mask: Image.Image) -> None:
    bbox = mask.getbbox()
    if bbox is None:
        raise SubjectSealError("subject_mask_empty")
    width, height = mask.size
    margin = max(1, round(min(width, height) * MIN_SUBJECT_EDGE_MARGIN_RATIO))
    left, top, right, bottom = bbox
    if (
        left < margin
        or top < margin
        or width - right < margin
        or height - bottom < margin
    ):
        raise SubjectSealError("subject_source_not_cleanly_extractable")


def _decode_mask(mask_png: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(mask_png)) as opened_mask:
            opened_mask.load()
            if opened_mask.mode != "L":
                raise SubjectSealError("subject_mask_mode_invalid")
            mask = opened_mask.copy()
    except SubjectSealError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise SubjectSealError("subject_mask_invalid") from error
    return mask


def _decode_review_mask(mask_png: bytes) -> Image.Image:
    """Decode a strict grayscale mask, including browser canvas RGBA output."""
    try:
        with Image.open(BytesIO(mask_png)) as opened_mask:
            opened_mask.load()
            if opened_mask.mode == "L":
                return opened_mask.copy()
            if opened_mask.mode != "RGBA":
                raise SubjectSealError("subject_mask_mode_invalid")
            rgba = opened_mask.copy()
    except SubjectSealError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise SubjectSealError("subject_mask_invalid") from error
    pixels = tuple(cast(Sequence[tuple[int, int, int, int]], rgba.get_flattened_data()))
    if any(
        red != green or green != blue or alpha != 255
        for red, green, blue, alpha in pixels
    ):
        raise SubjectSealError("subject_mask_mode_invalid")
    return rgba.getchannel("R")


def _seal_decoded_subject(
    source: Image.Image,
    mask: Image.Image,
    *,
    adapter_version: str,
    synthetic_test_only: bool,
) -> SealedSubject:
    alpha_values = tuple(cast(Sequence[int], mask.get_flattened_data()))
    if not any(alpha_values):
        raise SubjectSealError("subject_mask_empty")

    rgba = source.convert("RGBA")
    rgba.putalpha(mask)
    core = mask.point(lambda value: 255 if value >= CORE_ALPHA_THRESHOLD else 0)
    core_values = tuple(cast(Sequence[int], core.get_flattened_data()))
    core_pixel_count = sum(value == 255 for value in core_values)
    if core_pixel_count == 0:
        raise SubjectSealError("subject_mask_core_empty")

    rgba_png = _encode_png(rgba)
    core_mask_png = _encode_png(core)
    return SealedSubject(
        rgba_png=rgba_png,
        rgba_sha256=sha256(rgba_png).hexdigest(),
        core_mask_png=core_mask_png,
        core_mask_sha256=sha256(core_mask_png).hexdigest(),
        width=rgba.width,
        height=rgba.height,
        core_mask_pixel_count=core_pixel_count,
        feather_width_px=1 if any(0 < value < 255 for value in alpha_values) else 0,
        adapter_version=adapter_version,
        synthetic_test_only=synthetic_test_only,
        publishable=False,
    )


def _decode_source(request: SubjectSegmentationRequest) -> Image.Image:
    if (
        not request.content
        or sha256(request.content).hexdigest() != request.content_sha256
    ):
        raise SubjectSealError("subject_source_hash_mismatch")
    if request.mime_type not in SUPPORTED_SUBJECT_MIME_TYPES:
        raise SubjectSealError("subject_source_mime_unsupported")
    if (
        len(request.target_canvas) != 2
        or request.target_canvas[0] <= 0
        or request.target_canvas[1] <= 0
    ):
        raise SubjectSealError("subject_target_canvas_invalid")
    return _decode_image(request.content, "subject_source_invalid")


def _decode_image(content: bytes, error_code: str) -> Image.Image:
    try:
        with Image.open(BytesIO(content)) as opened:
            opened.load()
            return ImageOps.exif_transpose(opened).copy()
    except (OSError, UnidentifiedImageError) as error:
        raise SubjectSealError(error_code) from error


def _fit_to_canvas(image: Image.Image, canvas: tuple[int, int]) -> Image.Image:
    fitted_size = _fitted_size(image.size, canvas)
    return (
        image
        if fitted_size == image.size
        else image.resize(fitted_size, Image.Resampling.LANCZOS)
    )


def _fitted_size(size: tuple[int, int], canvas: tuple[int, int]) -> tuple[int, int]:
    width, height = size
    scale = min(canvas[0] / width, canvas[1] / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _encode_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
