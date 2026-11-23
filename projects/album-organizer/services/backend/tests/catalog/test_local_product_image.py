from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image, PngImagePlugin

import photo_organizer.catalog.local_product_image as image_module
from photo_organizer.catalog.local_product_image import (
    LocalProductImageError,
    validate_local_product_image,
)
from photo_organizer.schemas.real_sku_pilot import PilotImageIntent

IMAGE_ID = UUID("11111111-1111-4111-8111-111111111111")


def _intent() -> PilotImageIntent:
    return PilotImageIntent(
        local_image_id=IMAGE_ID,
        role="primary",
        position=1,
        captured_at=datetime(2026, 8, 17, 10, tzinfo=UTC),
        human_privacy_reviewed=True,
    )


def _write_jpeg(path: Path, *, exif: Image.Exif | None = None) -> Path:
    image = Image.new("RGB", (3, 3), "red")
    options = {"exif": exif} if exif is not None else {}
    image.save(path, format="JPEG", **options)
    return path


def _write_png(path: Path, *, metadata: dict[str, str] | None = None) -> Path:
    image = Image.new("RGB", (3, 3), "blue")
    pnginfo = PngImagePlugin.PngInfo()
    for key, value in (metadata or {}).items():
        pnginfo.add_text(key, value)
    image.save(path, format="PNG", pnginfo=pnginfo)
    return path


def _ftyp(major: bytes, compatible: tuple[bytes, ...] = ()) -> bytes:
    body = major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return (8 + len(body)).to_bytes(4, "big") + b"ftyp" + body


@pytest.mark.parametrize(
    ("writer", "media_type"),
    ((_write_jpeg, "image/jpeg"), (_write_png, "image/png")),
)
def test_valid_raster_returns_same_stream_hash_and_facts(
    tmp_path: Path,
    writer: object,
    media_type: str,
) -> None:
    path = tmp_path / "asset"
    writer(path)  # type: ignore[operator]

    result = validate_local_product_image(_intent(), path)

    content = path.read_bytes()
    assert result.media_type == media_type
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.byte_size == len(content)
    assert result.local_image_id == IMAGE_ID
    assert result.privacy_metadata_status == "no_sensitive_metadata_detected"


@pytest.mark.parametrize(
    ("content", "media_type"),
    (
        (_ftyp(b"heic", (b"isom",)), "image/heic"),
        (_ftyp(b"mif1", (b"msf1",)), "image/heif"),
        (_ftyp(b"isom", (b"isom",) * 13 + (b"heic",)), "image/heic"),
    ),
)
def test_valid_heif_family_ftyp_is_accepted(
    content: bytes,
    media_type: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "asset"
    path.write_bytes(content)

    result = validate_local_product_image(_intent(), path)

    assert result.media_type == media_type
    assert result.sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    "content",
    (
        b"",
        b"not-an-image",
        b"\xff\xd8\xff",
        b"\x00\x00\x00\x20ftypheic\x00\x00\x00\x00",
        b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00heic",
    ),
)
def test_malformed_or_forged_media_is_rejected(
    content: bytes,
    tmp_path: Path,
) -> None:
    path = tmp_path / "asset.jpg"
    path.write_bytes(content)

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "unsafe_image"
    assert str(path) not in str(failure.value)


def test_file_over_100_mib_is_rejected_before_copy(tmp_path: Path) -> None:
    path = tmp_path / "asset"
    with path.open("wb") as file:
        file.truncate(104_857_601)

    with pytest.raises(LocalProductImageError, match="unsafe_image"):
        validate_local_product_image(_intent(), path)


def test_raster_over_pixel_limit_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "compressed-large.png"
    Image.new("1", (7100, 7100)).save(path, "PNG")

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "unsafe_image"


def test_pillow_decompression_bomb_warning_is_a_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_png(tmp_path / "asset.png")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 8)

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "unsafe_image"


def test_symlink_is_rejected(tmp_path: Path) -> None:
    target = _write_jpeg(tmp_path / "target.jpg")
    link = tmp_path / "asset.jpg"
    link.symlink_to(target)

    with pytest.raises(LocalProductImageError, match="unsafe_image"):
        validate_local_product_image(_intent(), link)


def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "asset"
    os.mkfifo(fifo)

    with pytest.raises(LocalProductImageError, match="unsafe_image"):
        validate_local_product_image(_intent(), fifo)


def test_path_replacement_between_lstat_and_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_jpeg(tmp_path / "asset.jpg")
    replacement = _write_png(tmp_path / "replacement.png")
    original_open = image_module._open_source

    def replace_then_open(candidate: os.PathLike[str] | str, flags: int) -> int:
        path.unlink()
        replacement.rename(path)
        return original_open(candidate, flags)

    monkeypatch.setattr(image_module, "_open_source", replace_then_open)

    with pytest.raises(LocalProductImageError, match="unsafe_image"):
        validate_local_product_image(_intent(), path)


def test_source_open_uses_no_follow_and_nonblocking_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_png(tmp_path / "asset.png")
    original_open = image_module._open_source
    captured_flags: list[int] = []

    def capture_open(candidate: os.PathLike[str] | str, flags: int) -> int:
        captured_flags.append(flags)
        return original_open(candidate, flags)

    monkeypatch.setattr(image_module, "_open_source", capture_open)

    validate_local_product_image(_intent(), path)

    assert captured_flags == [
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    ]


def test_mutated_intent_instance_returns_only_safe_error(tmp_path: Path) -> None:
    path = _write_png(tmp_path / "asset.png")
    forged = _intent().model_copy(
        update={"captured_at": datetime(2026, 8, 17, tzinfo=None)}  # noqa: DTZ001
    )

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(forged, path)

    assert failure.value.code == "unsafe_image"
    assert str(failure.value) == "unsafe_image"


@pytest.mark.parametrize("tag", (34853, 315, 270, 37510, 42032, 42033))
def test_sensitive_jpeg_exif_is_rejected(tag: int, tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[tag] = {1: "N"} if tag == 34853 else "private-marker"
    path = _write_jpeg(tmp_path / "asset.jpg", exif=exif)

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "privacy_boundary_failed"
    assert "private-marker" not in str(failure.value)
    assert str(path) not in str(failure.value)


@pytest.mark.parametrize(
    "key", ("Comment", "Description", "Author", "XML:com.adobe.xmp")
)
def test_png_text_metadata_is_rejected(key: str, tmp_path: Path) -> None:
    path = _write_png(tmp_path / "asset.png", metadata={key: "private-marker"})

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "privacy_boundary_failed"
    assert "private-marker" not in str(failure.value)


def test_arbitrary_png_text_metadata_is_rejected(tmp_path: Path) -> None:
    path = _write_png(tmp_path / "asset.png", metadata={"Source": "private-marker"})

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "privacy_boundary_failed"
    assert "private-marker" not in str(failure.value)


def test_jpeg_xmp_metadata_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "asset.jpg"
    Image.new("RGB", (3, 3), "red").save(
        path,
        "JPEG",
        xmp=b"<x:xmpmeta>private-marker</x:xmpmeta>",
    )

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "privacy_boundary_failed"
    assert "private-marker" not in str(failure.value)


def test_jpeg_app13_iptc_metadata_is_rejected(tmp_path: Path) -> None:
    path = _write_jpeg(tmp_path / "asset.jpg")
    original = path.read_bytes()
    payload = b"Photoshop 3.0\x00private-marker"
    app13 = b"\xff\xed" + (len(payload) + 2).to_bytes(2, "big") + payload
    path.write_bytes(original[:2] + app13 + original[2:])

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "privacy_boundary_failed"
    assert "private-marker" not in str(failure.value)


@pytest.mark.parametrize(
    "marker",
    (b"Exif\x00\x00private-marker", b"<x:xmpmeta>private-marker</x:xmpmeta>"),
)
def test_heic_metadata_markers_are_rejected(marker: bytes, tmp_path: Path) -> None:
    metadata_box = (8 + len(marker)).to_bytes(4, "big") + b"uuid" + marker
    path = tmp_path / "asset.heic"
    path.write_bytes(_ftyp(b"heic") + metadata_box)

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "privacy_boundary_failed"
    assert "private-marker" not in str(failure.value)


def test_safe_error_contract_has_no_source_details(tmp_path: Path) -> None:
    path = tmp_path / "customer-phone-13800138000.jpg"

    with pytest.raises(LocalProductImageError) as failure:
        validate_local_product_image(_intent(), path)

    assert failure.value.code == "unsafe_image"
    assert str(failure.value) == "unsafe_image"
