"""Product-subject sealing contracts for identity-locked composition."""

from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from opc_style.domain.subject_seal import (
    HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION,
    DeterministicLocalSubjectSegmenter,
    SubjectSealError,
    SubjectSegmentationRequest,
    review_sealed_subject_mask,
    seal_subject_with_mask,
    validate_sealed_subject_for_generation,
)


def _png(
    mode: str,
    size: tuple[int, int],
    color: int | tuple[int, ...],
) -> bytes:
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _request(content: bytes, *, target: tuple[int, int] = (8, 10)) -> SubjectSegmentationRequest:
    return SubjectSegmentationRequest(
        content=content,
        content_sha256=sha256(content).hexdigest(),
        mime_type="image/png",
        target_canvas=target,
    )


def test_alpha_product_is_sealed_with_stable_hashes_and_binary_core() -> None:
    """Removing source-alpha handling would turn transparent pixels into product."""
    image = Image.new("RGBA", (3, 2), (20, 40, 60, 0))
    image.putpixel((1, 0), (200, 10, 30, 255))
    image.putpixel((1, 1), (210, 20, 40, 128))
    output = BytesIO()
    image.save(output, format="PNG")

    sealed = DeterministicLocalSubjectSegmenter().seal(_request(output.getvalue()))

    assert sha256(sealed.rgba_png).hexdigest() == sealed.rgba_sha256
    assert sha256(sealed.core_mask_png).hexdigest() == sealed.core_mask_sha256
    assert sealed.width == 3
    assert sealed.height == 2
    assert sealed.core_mask_pixel_count == 1
    assert sealed.feather_width_px == 1
    assert sealed.adapter_version == "deterministic-local-subject-v2"
    assert sealed.synthetic_test_only is True
    assert sealed.publishable is False
    with Image.open(BytesIO(sealed.core_mask_png)) as mask:
        assert mask.mode == "L"
        assert set(mask.get_flattened_data()) == {0, 255}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        ("hash", "subject_source_hash_mismatch"),
        ("mime", "subject_source_mime_unsupported"),
        ("target", "subject_target_canvas_invalid"),
    ],
)
def test_invalid_source_fails_closed(mutate: str, code: str) -> None:
    """Source identity and geometry cannot be guessed or silently normalized."""
    content = _png("RGBA", (3, 2), (10, 20, 30, 255))
    request = _request(content)
    if mutate == "hash":
        request = SubjectSegmentationRequest(
            content=content,
            content_sha256="0" * 64,
            mime_type="image/png",
            target_canvas=(8, 10),
        )
    elif mutate == "mime":
        request = SubjectSegmentationRequest(
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/gif",
            target_canvas=(8, 10),
        )
    else:
        request = _request(content, target=(0, 2))

    with pytest.raises(SubjectSealError) as raised:
        DeterministicLocalSubjectSegmenter().seal(request)

    assert raised.value.code == code


def test_large_source_is_normalized_once_before_its_pixels_are_sealed() -> None:
    """Rejecting ordinary camera dimensions would make the real-photo path unusable."""
    content = _png("RGBA", (12, 8), (10, 20, 30, 255))

    sealed = DeterministicLocalSubjectSegmenter().seal(
        _request(content, target=(6, 5))
    )

    assert (sealed.width, sealed.height) == (6, 4)
    assert sealed.core_mask_pixel_count == 24


@pytest.mark.parametrize(
    ("mask", "code"),
    [
        (_png("L", (3, 2), 0), "subject_mask_empty"),
        (_png("L", (2, 2), 255), "subject_mask_dimensions_mismatch"),
        (_png("RGB", (3, 2), (0, 255, 0)), "subject_mask_mode_invalid"),
    ],
)
def test_invalid_mask_fails_closed(mask: bytes, code: str) -> None:
    """An unusable visual mask must never fall back to the whole photograph."""
    request = _request(_png("RGB", (3, 2), (100, 80, 60)))

    with pytest.raises(SubjectSealError) as raised:
        seal_subject_with_mask(
            request,
            mask,
            adapter_version="visual-test-v1",
            synthetic_test_only=True,
        )

    assert raised.value.code == code


def test_gray_visual_mask_has_binary_core_and_preserves_feather_alpha() -> None:
    """Thresholding the edge into the protected core would reject valid feathering."""
    source = _png("RGB", (3, 1), (90, 100, 110))
    mask_image = Image.new("L", (3, 1))
    mask_image.putdata([0, 128, 255])
    mask_output = BytesIO()
    mask_image.save(mask_output, format="PNG")

    sealed = seal_subject_with_mask(
        _request(source),
        mask_output.getvalue(),
        adapter_version="visual-test-v1",
        synthetic_test_only=False,
    )

    with Image.open(BytesIO(sealed.rgba_png)) as rgba:
        assert [rgba.getpixel((x, 0))[3] for x in range(3)] == [0, 128, 255]
    with Image.open(BytesIO(sealed.core_mask_png)) as core:
        assert [core.getpixel((x, 0)) for x in range(3)] == [0, 0, 255]
    assert sealed.core_mask_pixel_count == 1
    assert sealed.adapter_version == "visual-test-v1"
    assert sealed.synthetic_test_only is False
    assert sealed.publishable is False


def test_nearly_opaque_feather_pixels_remain_outside_exact_core() -> None:
    """Alpha blending changes RGB until alpha is exactly 255."""
    source = _png("RGB", (3, 1), (90, 100, 110))
    mask_image = Image.new("L", (3, 1))
    mask_image.putdata([250, 254, 255])
    mask_output = BytesIO()
    mask_image.save(mask_output, format="PNG")

    sealed = seal_subject_with_mask(
        _request(source),
        mask_output.getvalue(),
        adapter_version="visual-test-v2",
        synthetic_test_only=False,
    )

    with Image.open(BytesIO(sealed.core_mask_png)) as core:
        assert [core.getpixel((x, 0)) for x in range(3)] == [0, 0, 255]
    assert sealed.core_mask_pixel_count == 1


def test_human_reviewed_mask_may_erase_but_never_reveal_pixels() -> None:
    """Browser correction is deletion, never a back door for invented pixels."""
    source = Image.new("RGBA", (10, 10), (90, 100, 110, 0))
    for point in ((4, 4), (5, 4), (4, 5)):
        source.putpixel(point, (190, 20, 10, 255))
    source_output = BytesIO()
    source.save(source_output, format="PNG")

    reviewed = Image.new("L", (10, 10))
    reviewed.putpixel((4, 4), 255)
    reviewed.putpixel((5, 4), 255)
    reviewed_output = BytesIO()
    reviewed.save(reviewed_output, format="PNG")
    sealed = review_sealed_subject_mask(
        source_output.getvalue(),
        reviewed_output.getvalue(),
        synthetic_test_only=False,
    )

    assert sealed.adapter_version == HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION
    assert sealed.core_mask_pixel_count == 2
    with Image.open(BytesIO(sealed.rgba_png)) as rgba:
        assert rgba.getpixel((4, 4))[3] == 255
        assert rgba.getpixel((4, 5))[3] == 0

    invalid = reviewed.copy()
    invalid.putpixel((8, 8), 255)
    invalid_output = BytesIO()
    invalid.save(invalid_output, format="PNG")
    with pytest.raises(SubjectSealError) as raised:
        review_sealed_subject_mask(
            source_output.getvalue(),
            invalid_output.getvalue(),
            synthetic_test_only=False,
        )
    assert raised.value.code == "subject_review_mask_may_only_erase"


def test_human_review_accepts_browser_canvas_grayscale_rgba_mask() -> None:
    """Canvas PNG export is RGBA even when its visible pixels are grayscale."""
    source_image = Image.new("RGBA", (10, 10), (90, 100, 110, 0))
    for point in ((4, 4), (5, 4), (4, 5)):
        source_image.putpixel(point, (90, 100, 110, 255))
    source_output = BytesIO()
    source_image.save(source_output, format="PNG")
    browser_mask = Image.new("RGBA", (10, 10), (0, 0, 0, 255))
    browser_mask.putpixel((4, 4), (255, 255, 255, 255))
    browser_mask.putpixel((5, 4), (255, 255, 255, 255))
    mask_output = BytesIO()
    browser_mask.save(mask_output, format="PNG")

    reviewed = review_sealed_subject_mask(
        source_output.getvalue(),
        mask_output.getvalue(),
        synthetic_test_only=True,
    )

    assert reviewed.core_mask_pixel_count == 2


def test_human_review_rejects_large_destructive_erase() -> None:
    source = Image.new("RGBA", (20, 20), (90, 100, 110, 0))
    for y in range(5, 15):
        for x in range(5, 15):
            source.putpixel((x, y), (190, 20, 10, 255))
    source_output = BytesIO()
    source.save(source_output, format="PNG")
    reviewed = Image.new("L", source.size)
    for y in range(5, 10):
        for x in range(5, 15):
            reviewed.putpixel((x, y), 255)
    mask_output = BytesIO()
    reviewed.save(mask_output, format="PNG")

    with pytest.raises(SubjectSealError) as raised:
        review_sealed_subject_mask(
            source_output.getvalue(),
            mask_output.getvalue(),
            synthetic_test_only=False,
        )
    assert raised.value.code == "subject_source_not_cleanly_extractable"


def test_persisted_subject_touching_canvas_edge_is_not_generation_ready() -> None:
    rgba = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    for y in range(5, 15):
        for x in range(8):
            rgba.putpixel((x, y), (190, 20, 10, 255))
    output = BytesIO()
    rgba.save(output, format="PNG")

    with pytest.raises(SubjectSealError) as raised:
        validate_sealed_subject_for_generation(output.getvalue())
    assert raised.value.code == "subject_source_not_cleanly_extractable"
