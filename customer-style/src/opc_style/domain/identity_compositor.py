"""Lossless local composition with exact protected-pixel attestation."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any

from PIL import Image, ImageFilter, UnidentifiedImageError

from opc_style.domain.subject_seal import SealedSubject

IDENTITY_COMPOSITOR_VERSION = "identity-locked-alpha-compositor-v2"
CONTACT_SHADOW_VERSION = "subject-alpha-contact-shadow-v1"


class IdentityCompositionError(RuntimeError):
    """Stable fail-closed local composition error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Placement:
    """Top-left location for an unchanged, native-size sealed subject."""

    x: int
    y: int


@dataclass(frozen=True)
class IdentityLockAttestation:
    """Exact comparison result for the binary protected subject core."""

    protected_pixels_match: bool
    protected_pixel_count: int
    code: str
    candidate_sha256: str
    subject_rgba_sha256: str
    core_mask_sha256: str
    compositor_version: str


@dataclass(frozen=True)
class IdentityLockedComposite:
    """One locally composed, non-publishable candidate."""

    content: bytes
    content_sha256: str
    mime_type: str
    width: int
    height: int
    placement: Placement
    metadata: dict[str, Any]
    attestation: IdentityLockAttestation
    synthetic_test_only: bool
    publishable: bool = False


def composite_subject(
    background: bytes,
    subject: SealedSubject,
    placement: Placement,
) -> IdentityLockedComposite:
    """Paste sealed RGBA once and prove its protected core survived exactly."""

    background_image = _decode_background(background)
    subject_image, core_mask = _decode_and_validate_subject(subject)
    _validate_geometry(background_image.size, subject_image.size, placement)

    canvas = background_image.convert("RGBA")
    _apply_contact_shadow(canvas, subject_image, placement)
    canvas.alpha_composite(subject_image, dest=(placement.x, placement.y))
    content = _encode_png(canvas)
    content_sha256 = sha256(content).hexdigest()
    metadata: dict[str, Any] = {
        "compositor_version": IDENTITY_COMPOSITOR_VERSION,
        "background_sha256": sha256(background).hexdigest(),
        "subject_rgba_sha256": subject.rgba_sha256,
        "core_mask_sha256": subject.core_mask_sha256,
        "protected_pixel_count": subject.core_mask_pixel_count,
        "placement_x": placement.x,
        "placement_y": placement.y,
        "subject_width": subject.width,
        "subject_height": subject.height,
        "contact_shadow_version": CONTACT_SHADOW_VERSION,
        "synthetic_test_only": subject.synthetic_test_only,
        "publishable": False,
    }
    provisional = IdentityLockedComposite(
        content=content,
        content_sha256=content_sha256,
        mime_type="image/png",
        width=canvas.width,
        height=canvas.height,
        placement=placement,
        metadata=metadata,
        attestation=_pending_attestation(subject, content_sha256),
        synthetic_test_only=subject.synthetic_test_only,
        publishable=False,
    )
    attestation = _attest_decoded(provisional, subject_image, core_mask, subject)
    if not attestation.protected_pixels_match:
        raise IdentityCompositionError(attestation.code)
    return IdentityLockedComposite(
        content=provisional.content,
        content_sha256=provisional.content_sha256,
        mime_type=provisional.mime_type,
        width=provisional.width,
        height=provisional.height,
        placement=provisional.placement,
        metadata=provisional.metadata,
        attestation=attestation,
        synthetic_test_only=provisional.synthetic_test_only,
        publishable=False,
    )


def attest_identity_lock(
    candidate: IdentityLockedComposite,
    subject: SealedSubject,
) -> IdentityLockAttestation:
    """Re-open stored bytes and compare every sealed binary-core RGB pixel."""

    subject_image, core_mask = _decode_and_validate_subject(subject)
    return _attest_decoded(candidate, subject_image, core_mask, subject)


def _attest_decoded(
    candidate: IdentityLockedComposite,
    subject_image: Image.Image,
    core_mask: Image.Image,
    subject: SealedSubject,
) -> IdentityLockAttestation:
    actual_hash = sha256(candidate.content).hexdigest()
    failure = _candidate_metadata_failure(candidate, subject, actual_hash)
    if failure is not None:
        return _failed_attestation(failure, actual_hash, subject)
    if candidate.mime_type != "image/png":
        return _failed_attestation(
            "identity_lock_candidate_not_lossless_png", actual_hash, subject
        )
    try:
        with Image.open(BytesIO(candidate.content)) as opened:
            opened.load()
            if opened.format != "PNG":
                return _failed_attestation(
                    "identity_lock_candidate_not_lossless_png", actual_hash, subject
                )
            image = opened.convert("RGB")
    except (OSError, UnidentifiedImageError):
        return _failed_attestation(
            "identity_lock_candidate_invalid", actual_hash, subject
        )
    if image.size != (candidate.width, candidate.height):
        return _failed_attestation(
            "identity_lock_candidate_dimensions_mismatch", actual_hash, subject
        )
    try:
        _validate_geometry(image.size, subject_image.size, candidate.placement)
    except IdentityCompositionError:
        return _failed_attestation(
            "identity_lock_placement_out_of_bounds", actual_hash, subject
        )

    source = subject_image.convert("RGB")
    protected_count = 0
    for subject_y in range(subject.height):
        for subject_x in range(subject.width):
            if core_mask.getpixel((subject_x, subject_y)) != 255:
                continue
            protected_count += 1
            candidate_pixel = image.getpixel(
                (candidate.placement.x + subject_x, candidate.placement.y + subject_y)
            )
            if candidate_pixel != source.getpixel((subject_x, subject_y)):
                return _failed_attestation(
                    "identity_lock_pixel_mismatch", actual_hash, subject, protected_count
                )
    if protected_count != subject.core_mask_pixel_count:
        return _failed_attestation(
            "identity_lock_protected_count_mismatch", actual_hash, subject, protected_count
        )
    return IdentityLockAttestation(
        protected_pixels_match=True,
        protected_pixel_count=protected_count,
        code="identity_lock_passed",
        candidate_sha256=actual_hash,
        subject_rgba_sha256=subject.rgba_sha256,
        core_mask_sha256=subject.core_mask_sha256,
        compositor_version=IDENTITY_COMPOSITOR_VERSION,
    )


def _candidate_metadata_failure(
    candidate: IdentityLockedComposite,
    subject: SealedSubject,
    actual_hash: str,
) -> str | None:
    if candidate.content_sha256 != actual_hash:
        return "identity_lock_candidate_hash_mismatch"
    expected = {
        "compositor_version": IDENTITY_COMPOSITOR_VERSION,
        "subject_rgba_sha256": subject.rgba_sha256,
        "core_mask_sha256": subject.core_mask_sha256,
        "protected_pixel_count": subject.core_mask_pixel_count,
        "placement_x": candidate.placement.x,
        "placement_y": candidate.placement.y,
        "subject_width": subject.width,
        "subject_height": subject.height,
        "contact_shadow_version": CONTACT_SHADOW_VERSION,
        "publishable": False,
    }
    if any(candidate.metadata.get(key) != value for key, value in expected.items()):
        if (
            candidate.metadata.get("placement_x") != candidate.placement.x
            or candidate.metadata.get("placement_y") != candidate.placement.y
        ):
            return "identity_lock_placement_metadata_mismatch"
        return "identity_lock_metadata_mismatch"
    if candidate.publishable or candidate.metadata.get("publishable") is not False:
        return "identity_lock_publishable_forbidden"
    return None


def _apply_contact_shadow(
    canvas: Image.Image,
    subject_image: Image.Image,
    placement: Placement,
) -> None:
    """Add a subdued local shadow without changing any protected product pixel."""

    alpha = subject_image.getchannel("A")
    blur_radius = max(6, round(min(subject_image.size) * 0.018))
    vertical_offset = max(4, round(subject_image.height * 0.012))
    shadow_alpha = alpha.filter(ImageFilter.GaussianBlur(blur_radius)).point(
        lambda value: round(value * 0.16)
    )
    shadow = Image.new("RGBA", subject_image.size, (24, 20, 17, 0))
    shadow.putalpha(shadow_alpha)
    shadow_y = min(
        placement.y + vertical_offset,
        canvas.height - subject_image.height,
    )
    canvas.alpha_composite(shadow, dest=(placement.x, shadow_y))


def _decode_background(content: bytes) -> Image.Image:
    try:
        with Image.open(BytesIO(content)) as opened:
            opened.load()
            if opened.format not in {"JPEG", "PNG"}:
                raise IdentityCompositionError("background_format_unsupported")
            return opened.copy()
    except IdentityCompositionError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise IdentityCompositionError("background_invalid") from error


def _decode_and_validate_subject(
    subject: SealedSubject,
) -> tuple[Image.Image, Image.Image]:
    if sha256(subject.rgba_png).hexdigest() != subject.rgba_sha256:
        raise IdentityCompositionError("subject_rgba_hash_mismatch")
    if sha256(subject.core_mask_png).hexdigest() != subject.core_mask_sha256:
        raise IdentityCompositionError("subject_core_mask_hash_mismatch")
    try:
        with Image.open(BytesIO(subject.rgba_png)) as opened:
            opened.load()
            if opened.format != "PNG" or opened.mode != "RGBA":
                raise IdentityCompositionError("subject_rgba_invalid")
            rgba = opened.copy()
    except IdentityCompositionError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise IdentityCompositionError("subject_rgba_invalid") from error
    try:
        with Image.open(BytesIO(subject.core_mask_png)) as opened:
            opened.load()
            if opened.format != "PNG" or opened.mode != "L":
                raise IdentityCompositionError("subject_core_mask_invalid")
            mask = opened.copy()
    except IdentityCompositionError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise IdentityCompositionError("subject_core_mask_invalid") from error
    expected_size = (subject.width, subject.height)
    if rgba.size != expected_size or mask.size != expected_size:
        raise IdentityCompositionError("subject_dimensions_mismatch")
    pixel_count = sum(
        mask.getpixel((x, y)) == 255
        for y in range(mask.height)
        for x in range(mask.width)
    )
    if pixel_count != subject.core_mask_pixel_count or pixel_count == 0:
        raise IdentityCompositionError("subject_core_mask_count_mismatch")
    return rgba, mask


def _validate_geometry(
    background_size: tuple[int, int],
    subject_size: tuple[int, int],
    placement: Placement,
) -> None:
    if subject_size[0] > background_size[0] or subject_size[1] > background_size[1]:
        raise IdentityCompositionError("subject_exceeds_background")
    if (
        placement.x < 0
        or placement.y < 0
        or placement.x + subject_size[0] > background_size[0]
        or placement.y + subject_size[1] > background_size[1]
    ):
        raise IdentityCompositionError("placement_out_of_bounds")


def _encode_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _pending_attestation(
    subject: SealedSubject, candidate_sha256: str
) -> IdentityLockAttestation:
    return IdentityLockAttestation(
        False,
        0,
        "identity_lock_pending",
        candidate_sha256,
        subject.rgba_sha256,
        subject.core_mask_sha256,
        IDENTITY_COMPOSITOR_VERSION,
    )


def _failed_attestation(
    code: str,
    candidate_sha256: str,
    subject: SealedSubject,
    protected_pixel_count: int = 0,
) -> IdentityLockAttestation:
    return IdentityLockAttestation(
        protected_pixels_match=False,
        protected_pixel_count=protected_pixel_count,
        code=code,
        candidate_sha256=candidate_sha256,
        subject_rgba_sha256=subject.rgba_sha256,
        core_mask_sha256=subject.core_mask_sha256,
        compositor_version=IDENTITY_COMPOSITOR_VERSION,
    )
