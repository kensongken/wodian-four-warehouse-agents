from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pillow_heif import open_heif

_MAX_IMAGE_PIXELS = 50_000_000
_MAX_FTYP_BYTES = 4096
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx"}


class MediaSniffError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SniffedImage:
    media_type: str
    extension: str


def sniff_image(source: Path) -> SniffedImage:
    try:
        with source.open("rb") as stream:
            prefix = stream.read(16)
        if prefix.startswith(b"\xff\xd8\xff"):
            _verify_raster(source, "JPEG")
            return SniffedImage(media_type="image/jpeg", extension="jpg")
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            _verify_raster(source, "PNG")
            return SniffedImage(media_type="image/png", extension="png")
        if _has_heic_brand(source):
            _verify_heic(source)
            return SniffedImage(media_type="image/heic", extension="heic")
    except MediaSniffError:
        raise
    except (
        EOFError,
        OSError,
        RuntimeError,
        SyntaxError,
        TypeError,
        UnidentifiedImageError,
        ValueError,
    ):
        raise MediaSniffError("invalid_image") from None
    raise MediaSniffError("unsupported_media")


def _verify_raster(source: Path, expected_format: str) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if (
                    image.format != expected_format
                    or image.width * image.height > _MAX_IMAGE_PIXELS
                ):
                    raise MediaSniffError("invalid_image")
                image.verify()
            with Image.open(source) as image:
                if image.width * image.height > _MAX_IMAGE_PIXELS:
                    raise MediaSniffError("invalid_image")
                image.load()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise MediaSniffError("invalid_image") from None
    except (OSError, ValueError, TypeError, UnidentifiedImageError):
        raise MediaSniffError("invalid_image") from None


def _has_heic_brand(source: Path) -> bool:
    total_size = source.stat().st_size
    if total_size < 16:
        return False
    with source.open("rb") as stream:
        header = stream.read(16)
        box_size = int.from_bytes(header[:4], "big")
        if (
            header[4:8] != b"ftyp"
            or box_size < 16
            or box_size > min(total_size, _MAX_FTYP_BYTES)
            or box_size % 4
        ):
            return False
        compatible_brands = stream.read(box_size - 16)
        if len(compatible_brands) != box_size - 16:
            return False
        return header[8:12] in _HEIC_BRANDS or any(
            compatible_brands[index : index + 4] in _HEIC_BRANDS
            for index in range(0, len(compatible_brands), 4)
        )


def _verify_heic(source: Path) -> None:
    image = open_heif(source)
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
        raise MediaSniffError("invalid_image")
    if not image.data:
        raise MediaSniffError("invalid_image")
