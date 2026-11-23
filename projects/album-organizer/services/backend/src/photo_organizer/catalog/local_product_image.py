from __future__ import annotations

import os
import stat
import tempfile
import warnings
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from photo_organizer.schemas.real_sku_pilot import (
    PilotImageIntent,
    PilotMediaType,
    ValidatedPilotImage,
)

_MAX_IMAGE_BYTES = 104_857_600
_MAX_IMAGE_PIXELS = 50_000_000
_CHUNK_SIZE = 128 * 1024
_SENSITIVE_EXIF_TAGS = {270, 315, 34853, 37510, 42032, 42033}
_SENSITIVE_TEXT_KEYS = {
    "author",
    "comment",
    "copyright",
    "description",
    "iptc",
    "location",
    "photoshop",
    "xmp",
    "xml:com.adobe.xmp",
}
_HEIF_PRIVACY_MARKERS = (
    b"exif",
    b"<x:xmpmeta",
    b"http://ns.adobe.com/xap/1.0/",
    b"adobe:ns:meta/",
    b"uuid",
)

_open_source = os.open


class LocalProductImageError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_local_product_image(
    intent: PilotImageIntent,
    path: Path,
) -> ValidatedPilotImage:
    try:
        checked_intent = PilotImageIntent.model_validate(
            intent.model_dump(mode="json", warnings=False)
        )
        source_stat = path.lstat()
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise LocalProductImageError("unsafe_image")
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = _open_source(path, flags)
        with (
            os.fdopen(descriptor, "rb") as source,
            tempfile.TemporaryFile(mode="w+b") as snapshot,
        ):
            opened = os.fstat(source.fileno())
            _require_same_regular_file(source_stat, opened)
            _require_size(opened.st_size)
            digest, media_type, privacy_marker = _copy_hash_and_detect(
                source,
                snapshot,
                opened.st_size,
            )
            _reject_sensitive_metadata(snapshot, media_type, privacy_marker)
        return ValidatedPilotImage(
            local_image_id=checked_intent.local_image_id,
            role=checked_intent.role,
            position=checked_intent.position,
            captured_at=checked_intent.captured_at,
            human_privacy_reviewed=True,
            sha256=digest,
            byte_size=opened.st_size,
            media_type=media_type,
            privacy_metadata_status="no_sensitive_metadata_detected",
        )
    except LocalProductImageError:
        raise
    except (OSError, ValueError, TypeError, UnidentifiedImageError, ValidationError):
        raise LocalProductImageError("unsafe_image") from None


def _require_same_regular_file(
    expected: os.stat_result,
    opened: os.stat_result,
) -> None:
    if (
        not stat.S_ISREG(opened.st_mode)
        or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)
        or expected.st_size != opened.st_size
    ):
        raise LocalProductImageError("unsafe_image")


def _require_size(byte_size: int) -> None:
    if not 1 <= byte_size <= _MAX_IMAGE_BYTES:
        raise LocalProductImageError("unsafe_image")


def _copy_hash_and_detect(
    source: BinaryIO,
    snapshot: BinaryIO,
    byte_size: int,
) -> tuple[str, PilotMediaType, bool]:
    digest = sha256()
    prefix = bytearray()
    bytes_read = 0
    heic_probe = _IsoBrandProbe("image/heic", byte_size)
    heif_probe = _IsoBrandProbe("image/heif", byte_size)
    marker_probe = _MarkerProbe(_HEIF_PRIVACY_MARKERS)
    remaining = byte_size
    while remaining:
        chunk = source.read(min(_CHUNK_SIZE, remaining))
        if not chunk:
            break
        digest.update(chunk)
        snapshot.write(chunk)
        bytes_read += len(chunk)
        remaining -= len(chunk)
        if len(prefix) < 16:
            prefix.extend(chunk[: 16 - len(prefix)])
        heic_probe.update(chunk)
        heif_probe.update(chunk)
        marker_probe.update(chunk)
    if bytes_read != byte_size or source.read(1):
        raise LocalProductImageError("unsafe_image")
    snapshot.flush()
    media_type = _detect_media_type(
        bytes(prefix),
        heic_probe,
        heif_probe,
        snapshot,
    )
    return digest.hexdigest(), media_type, marker_probe.matched


def _detect_media_type(
    prefix: bytes,
    heic_probe: _IsoBrandProbe,
    heif_probe: _IsoBrandProbe,
    snapshot: BinaryIO,
) -> PilotMediaType:
    if prefix.startswith(b"\xff\xd8\xff"):
        _verify_raster(snapshot, "JPEG")
        return "image/jpeg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        _verify_raster(snapshot, "PNG")
        return "image/png"
    if heic_probe.matches():
        _validate_isobmff_boxes(snapshot)
        return "image/heic"
    if heif_probe.matches():
        _validate_isobmff_boxes(snapshot)
        return "image/heif"
    raise LocalProductImageError("unsafe_image")


def _verify_raster(snapshot: BinaryIO, expected_format: str) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            snapshot.seek(0)
            with Image.open(snapshot) as image:
                if (
                    image.format != expected_format
                    or image.width * image.height > _MAX_IMAGE_PIXELS
                ):
                    raise LocalProductImageError("unsafe_image")
                image.verify()
            snapshot.seek(0)
            with Image.open(snapshot) as image:
                if image.width * image.height > _MAX_IMAGE_PIXELS:
                    raise LocalProductImageError("unsafe_image")
                image.load()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise LocalProductImageError("unsafe_image") from None


def _reject_sensitive_metadata(
    snapshot: BinaryIO,
    media_type: PilotMediaType,
    heif_privacy_marker: bool,
) -> None:
    if media_type in {"image/heic", "image/heif"}:
        if heif_privacy_marker:
            raise LocalProductImageError("privacy_boundary_failed")
        return
    snapshot.seek(0)
    try:
        with Image.open(snapshot) as image:
            image.load()
            if _SENSITIVE_EXIF_TAGS.intersection(image.getexif().keys()):
                raise LocalProductImageError("privacy_boundary_failed")
            if media_type == "image/png" and getattr(image, "text", {}):
                raise LocalProductImageError("privacy_boundary_failed")
            if any(marker == "APP13" for marker, _ in getattr(image, "applist", ())):
                raise LocalProductImageError("privacy_boundary_failed")
            if any(
                isinstance(key, str) and key.casefold() in _SENSITIVE_TEXT_KEYS
                for key in image.info
            ):
                raise LocalProductImageError("privacy_boundary_failed")
    except LocalProductImageError:
        raise
    except (OSError, ValueError, TypeError, UnidentifiedImageError):
        raise LocalProductImageError("unsafe_image") from None


def _validate_isobmff_boxes(snapshot: BinaryIO) -> None:
    snapshot.seek(0, os.SEEK_END)
    total_size = snapshot.tell()
    offset = 0
    first = True
    while offset < total_size:
        snapshot.seek(offset)
        header = snapshot.read(8)
        if len(header) != 8:
            raise LocalProductImageError("unsafe_image")
        box_size = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        header_size = 8
        if box_size == 1:
            extended = snapshot.read(8)
            if len(extended) != 8:
                raise LocalProductImageError("unsafe_image")
            box_size = int.from_bytes(extended, "big")
            header_size = 16
        elif box_size == 0:
            box_size = total_size - offset
        if (
            box_size < header_size
            or offset + box_size > total_size
            or (first and box_type != b"ftyp")
        ):
            raise LocalProductImageError("unsafe_image")
        first = False
        offset += box_size
    if first or offset != total_size:
        raise LocalProductImageError("unsafe_image")


class _MarkerProbe:
    def __init__(self, markers: tuple[bytes, ...]) -> None:
        self._markers = tuple(marker.lower() for marker in markers)
        self._carry = b""
        self.matched = False

    def update(self, chunk: bytes) -> None:
        combined = (self._carry + chunk).lower()
        if any(marker in combined for marker in self._markers):
            self.matched = True
        carry_size = max(len(marker) for marker in self._markers) - 1
        self._carry = combined[-carry_size:]


class _IsoBrandProbe:
    def __init__(self, media_type: PilotMediaType, total_size: int) -> None:
        self._media_type = media_type
        self._total_size = total_size
        self._prefix = bytearray()
        self._box_size: int | None = None
        self._compatible_bytes = 0
        self._compatible_carry = b""
        self._matched = False
        self._invalid = False

    def update(self, chunk: bytes) -> None:
        position = 0
        if len(self._prefix) < 16:
            required = 16 - len(self._prefix)
            self._prefix.extend(chunk[:required])
            position = min(required, len(chunk))
            if len(self._prefix) < 16:
                return
            if self._prefix[4:8] != b"ftyp":
                self._invalid = True
                return
            box_size = int.from_bytes(self._prefix[:4], byteorder="big")
            if box_size < 16 or box_size > self._total_size or box_size % 4:
                self._invalid = True
                return
            self._box_size = box_size
            if bytes(self._prefix[8:12]) in self._allowed_brands:
                self._matched = True
        if self._invalid or self._box_size is None:
            return
        remaining = self._box_size - 16 - self._compatible_bytes
        compatible = chunk[position : position + remaining]
        self._compatible_bytes += len(compatible)
        combined = self._compatible_carry + compatible
        complete_length = len(combined) - len(combined) % 4
        if any(
            combined[index : index + 4] in self._allowed_brands
            for index in range(0, complete_length, 4)
        ):
            self._matched = True
        self._compatible_carry = combined[complete_length:]

    @property
    def _allowed_brands(self) -> set[bytes]:
        if self._media_type == "image/heic":
            return {b"heic", b"heix", b"hevc", b"hevx"}
        return {b"mif1", b"msf1"}

    def matches(self) -> bool:
        return (
            not self._invalid
            and self._box_size is not None
            and self._compatible_bytes == self._box_size - 16
            and not self._compatible_carry
            and self._matched
        )
