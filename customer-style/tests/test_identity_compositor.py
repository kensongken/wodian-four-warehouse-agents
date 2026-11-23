"""Lossless local compositing and protected-pixel attestation."""

from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from opc_style.domain.identity_compositor import (
    IdentityCompositionError,
    Placement,
    attest_identity_lock,
    composite_subject,
)
from opc_style.domain.subject_seal import SealedSubject


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _jpeg(size: tuple[int, int] = (6, 6)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (235, 230, 225)).save(output, format="JPEG")
    return output.getvalue()


def _subject() -> SealedSubject:
    rgba = Image.new("RGBA", (2, 2))
    rgba.putdata(
        [
            (255, 0, 0, 255),
            (0, 255, 0, 128),
            (0, 0, 255, 255),
            (255, 255, 0, 0),
        ]
    )
    mask = Image.new("L", (2, 2))
    mask.putdata([255, 0, 255, 0])
    rgba_png = _png(rgba)
    mask_png = _png(mask)
    return SealedSubject(
        rgba_png=rgba_png,
        rgba_sha256=sha256(rgba_png).hexdigest(),
        core_mask_png=mask_png,
        core_mask_sha256=sha256(mask_png).hexdigest(),
        width=2,
        height=2,
        core_mask_pixel_count=2,
        feather_width_px=1,
        adapter_version="fixture-subject-v1",
        synthetic_test_only=True,
        publishable=False,
    )


def test_composite_preserves_every_protected_rgb_pixel_exactly() -> None:
    """The core product colors are copied losslessly, never regenerated."""
    subject = _subject()
    composite = composite_subject(_jpeg(), subject, Placement(x=2, y=1))

    attestation = attest_identity_lock(composite, subject)

    assert attestation.protected_pixels_match is True
    assert attestation.code == "identity_lock_passed"
    assert attestation.protected_pixel_count == 2
    assert composite.mime_type == "image/png"
    assert (composite.width, composite.height) == (6, 6)
    assert composite.synthetic_test_only is True
    assert composite.publishable is False
    assert composite.metadata["contact_shadow_version"] == (
        "subject-alpha-contact-shadow-v1"
    )
    with Image.open(BytesIO(composite.content)) as image:
        pixels = image.convert("RGB")
        assert pixels.getpixel((2, 1)) == (255, 0, 0)
        assert pixels.getpixel((2, 2)) == (0, 0, 255)


def test_one_protected_pixel_tamper_fails_attestation() -> None:
    """One altered product pixel is enough to reject the whole candidate."""
    subject = _subject()
    composite = composite_subject(_jpeg(), subject, Placement(x=2, y=1))
    with Image.open(BytesIO(composite.content)) as image:
        tampered = image.convert("RGB")
    tampered.putpixel((2, 1), (254, 0, 0))
    content = _png(tampered)

    attestation = attest_identity_lock(
        replace(
            composite,
            content=content,
            content_sha256=sha256(content).hexdigest(),
        ),
        subject,
    )

    assert attestation.protected_pixels_match is False
    assert attestation.code == "identity_lock_pixel_mismatch"


@pytest.mark.parametrize(
    ("background_size", "placement", "code"),
    [
        ((1, 1), Placement(0, 0), "subject_exceeds_background"),
        ((6, 6), Placement(-1, 0), "placement_out_of_bounds"),
        ((6, 6), Placement(5, 5), "placement_out_of_bounds"),
    ],
)
def test_invalid_geometry_fails_closed(
    background_size: tuple[int, int], placement: Placement, code: str
) -> None:
    with pytest.raises(IdentityCompositionError) as error:
        composite_subject(_jpeg(background_size), _subject(), placement)

    assert error.value.code == code


def test_subject_hash_mismatch_is_rejected() -> None:
    subject = replace(_subject(), rgba_sha256="0" * 64)

    with pytest.raises(IdentityCompositionError) as error:
        composite_subject(_jpeg(), subject, Placement(2, 1))

    assert error.value.code == "subject_rgba_hash_mismatch"


def test_corrupt_mask_is_rejected() -> None:
    corrupt = b"not-a-png"
    subject = replace(
        _subject(),
        core_mask_png=corrupt,
        core_mask_sha256=sha256(corrupt).hexdigest(),
    )

    with pytest.raises(IdentityCompositionError) as error:
        composite_subject(_jpeg(), subject, Placement(2, 1))

    assert error.value.code == "subject_core_mask_invalid"


def test_jpeg_candidate_and_wrong_placement_metadata_are_rejected() -> None:
    subject = _subject()
    composite = composite_subject(_jpeg(), subject, Placement(2, 1))
    jpeg_candidate = _jpeg()

    jpeg_result = attest_identity_lock(
        replace(
            composite,
            content=jpeg_candidate,
            content_sha256=sha256(jpeg_candidate).hexdigest(),
            mime_type="image/jpeg",
        ),
        subject,
    )
    metadata_result = attest_identity_lock(
        replace(composite, metadata={**composite.metadata, "placement_x": 3}),
        subject,
    )

    assert jpeg_result.code == "identity_lock_candidate_not_lossless_png"
    assert metadata_result.code == "identity_lock_placement_metadata_mismatch"


def test_feather_pixels_are_not_counted_as_protected_core() -> None:
    """A semi-transparent edge may blend; only the sealed binary core is exact."""
    subject = _subject()
    composite = composite_subject(_jpeg(), subject, Placement(2, 1))
    with Image.open(BytesIO(composite.content)) as image:
        changed = image.convert("RGBA")
    changed.putpixel((3, 1), (9, 8, 7, 255))
    content = _png(changed)

    result = attest_identity_lock(
        replace(
            composite,
            content=content,
            content_sha256=sha256(content).hexdigest(),
        ),
        subject,
    )

    assert result.protected_pixels_match is True
    assert result.protected_pixel_count == 2
