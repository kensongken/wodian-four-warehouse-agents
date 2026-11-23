from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pillow_heif import from_pillow

from photo_organizer.archive.media_sniffer import MediaSniffError, sniff_image


def _minimal_heic() -> bytes:
    target = BytesIO()
    from_pillow(Image.new("RGB", (3, 2), "red")).save(target)
    return target.getvalue()


@pytest.mark.parametrize(
    ("image_format", "expected_media_type", "expected_extension"),
    (("JPEG", "image/jpeg", "jpg"), ("PNG", "image/png", "png")),
)
def test_sniffer_decodes_supported_raster_images(
    tmp_path: Path,
    image_format: str,
    expected_media_type: str,
    expected_extension: str,
) -> None:
    source = tmp_path / "extension-is-untrusted.bin"
    Image.new("RGB", (3, 2), "red").save(source, format=image_format)

    result = sniff_image(source)

    assert (result.media_type, result.extension) == (
        expected_media_type,
        expected_extension,
    )


@pytest.mark.parametrize(
    "content",
    (
        b"\xff\xd8\xff\xe0not-a-jpeg",
        b"\x89PNG\r\n\x1a\nnot-a-png",
    ),
)
def test_sniffer_rejects_forged_raster_headers(
    tmp_path: Path,
    content: bytes,
) -> None:
    source = tmp_path / "forged-image"
    source.write_bytes(content)

    with pytest.raises(MediaSniffError) as failure:
        sniff_image(source)

    assert failure.value.code == "invalid_image"
    assert str(failure.value) == "invalid_image"
    assert str(source) not in str(failure.value)


def test_sniffer_accepts_a_minimal_heic_image_structure(tmp_path: Path) -> None:
    source = tmp_path / "misleading.jpg"
    source.write_bytes(_minimal_heic())

    result = sniff_image(source)

    assert (result.media_type, result.extension) == ("image/heic", "heic")


def test_sniffer_does_not_treat_the_ftyp_minor_version_as_a_heic_brand(
    tmp_path: Path,
) -> None:
    source = tmp_path / "forged-minor-version.heic"
    forged = bytearray(_minimal_heic())
    ftyp_size = int.from_bytes(forged[:4], "big")
    forged[8:12] = b"isom"
    forged[12:16] = b"heic"
    for index in range(16, ftyp_size, 4):
        if bytes(forged[index : index + 4]) in {
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
        }:
            forged[index : index + 4] = b"isom"
    source.write_bytes(forged)

    with pytest.raises(MediaSniffError) as failure:
        sniff_image(source)

    assert failure.value.code == "unsupported_media"


@pytest.mark.parametrize(
    ("content", "expected_code"),
    (
        (b"not-an-image", "unsupported_media"),
        (b"\x00\x00\x00\x10ftypheic\x00\x00\x00\x00", "invalid_image"),
        (b"\x00\x00\x00\x20ftypheic\x00\x00\x00\x00", "unsupported_media"),
        (b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00heic", "unsupported_media"),
    ),
)
def test_sniffer_rejects_unsupported_or_forged_content(
    tmp_path: Path,
    content: bytes,
    expected_code: str,
) -> None:
    source = tmp_path / "product.heic"
    source.write_bytes(content)

    with pytest.raises(MediaSniffError) as failure:
        sniff_image(source)

    assert failure.value.code == expected_code
