from __future__ import annotations

import stat
import struct
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import BinaryIO

from photo_organizer.archive.media_sniffer import MediaSniffError, sniff_image
from photo_organizer.domain.limits import MAX_IMAGE_BYTES

MAX_ARCHIVE_EXPANDED_BYTES = 30 * 1024**3
MAX_ARCHIVE_ENTRIES = 10_000
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024**2
MAX_DISPLAY_NAME_CHARS = 1024
READ_CHUNK_BYTES = 1024 * 1024
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_MAX_EOCD_TAIL_BYTES = 22 + 65_535


class ArchiveSafetyError(ValueError):
    """A stable, non-sensitive reason to reject an untrusted archive."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _EntryReadError(ArchiveSafetyError):
    def __init__(self, code: str, byte_size: int) -> None:
        super().__init__(code)
        self.byte_size = byte_size


@dataclass(frozen=True)
class ScannedEntry:
    entry_index: int
    name: str
    declared_byte_size: int
    byte_size: int
    media_type: str | None = None
    sha256: str | None = None
    staged_path: Path | None = None
    issue_code: str | None = None


@dataclass
class _ArchiveReadBudget:
    expanded_bytes: int = 0

    def consume(self, byte_count: int) -> None:
        self.expanded_bytes += byte_count
        if self.expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ArchiveSafetyError("expanded_size_exceeded")


class ArchiveScanner:
    def scan(self, source: Path) -> Iterator[ScannedEntry]:
        budget = _ArchiveReadBudget()
        try:
            _preflight_archive(source)
            archive = zipfile.ZipFile(source)
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise ArchiveSafetyError("invalid_archive") from error
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ArchiveSafetyError("entry_limit_exceeded")
            with TemporaryDirectory(prefix="photo-organizer-archive-") as staging:
                for index, info in enumerate(infos):
                    try:
                        _validate_info(info)
                    except ArchiveSafetyError as error:
                        yield _issue_entry(
                            index,
                            info,
                            error.code,
                            hide_name=error.code
                            in {"unsafe_path", "link_entry", "unsafe_file_type"},
                        )
                        continue
                    if info.is_dir():
                        continue
                    if _is_system_metadata(info.filename):
                        yield _issue_entry(index, info, "system_metadata")
                        continue
                    staged_path = Path(staging) / f"entry-{index}"
                    try:
                        byte_size, digest = _stream_entry(
                            archive,
                            info,
                            staged_path,
                            budget=budget,
                        )
                    except _EntryReadError as error:
                        staged_path.unlink(missing_ok=True)
                        yield _issue_entry(
                            index,
                            info,
                            error.code,
                            byte_size=error.byte_size,
                        )
                        if error.code == "expanded_size_exceeded":
                            raise ArchiveSafetyError(error.code)
                        continue
                    with staged_path.open("rb") as staged:
                        header = staged.read(8)
                    if header.startswith(b"PK\x03\x04"):
                        staged_path.unlink(missing_ok=True)
                        yield _issue_entry(
                            index,
                            info,
                            "nested_archive",
                            byte_size=byte_size,
                            digest=digest,
                        )
                        continue
                    try:
                        sniffed = sniff_image(staged_path)
                    except MediaSniffError as error:
                        staged_path.unlink(missing_ok=True)
                        yield _issue_entry(
                            index,
                            info,
                            error.code,
                            byte_size=byte_size,
                            digest=digest,
                        )
                        continue
                    yield ScannedEntry(
                        entry_index=index,
                        name=_display_name(info.filename, index),
                        declared_byte_size=info.file_size,
                        byte_size=byte_size,
                        media_type=sniffed.media_type,
                        sha256=digest,
                        staged_path=staged_path,
                    )
                    staged_path.unlink(missing_ok=True)


def _issue_entry(
    index: int,
    info: zipfile.ZipInfo,
    code: str,
    *,
    hide_name: bool = False,
    byte_size: int = 0,
    digest: str | None = None,
) -> ScannedEntry:
    return ScannedEntry(
        entry_index=index,
        name=f"entry-{index}" if hide_name else _display_name(info.filename, index),
        declared_byte_size=info.file_size,
        byte_size=byte_size,
        sha256=digest,
        issue_code=code,
    )


def _is_system_metadata(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return "__MACOSX" in parts or (
        bool(parts) and parts[-1].casefold() in {".ds_store", "thumbs.db"}
    )


def _display_name(name: str, index: int) -> str:
    normalized = unicodedata.normalize("NFC", name)
    cleaned = "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith("C")
    )[:MAX_DISPLAY_NAME_CHARS]
    return cleaned or f"entry-{index}"


def _validate_info(info: zipfile.ZipInfo) -> None:
    original_name = getattr(info, "orig_filename", info.filename)
    path = PurePosixPath(info.filename)
    if (
        not info.filename
        or "\x00" in original_name
        or "\\" in info.filename
        or path.is_absolute()
        or ".." in path.parts
        or ":" in path.parts[0]
    ):
        raise ArchiveSafetyError("unsafe_path")
    if info.flag_bits & 0x1:
        raise ArchiveSafetyError("encrypted_entry")
    if info.flag_bits & 0x60:
        raise ArchiveSafetyError("unsupported_zip_feature")
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise ArchiveSafetyError("unsupported_compression")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        raise ArchiveSafetyError("link_entry")
    if file_type == stat.S_IFDIR and info.is_dir():
        return
    if file_type not in {0, stat.S_IFREG}:
        raise ArchiveSafetyError("unsafe_file_type")


def _stream_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    *,
    budget: _ArchiveReadBudget,
) -> tuple[int, str]:
    byte_size = 0
    digest = sha256()
    try:
        with archive.open(info) as entry, target.open("xb") as staged:
            while chunk := entry.read(_next_read_size(byte_size, budget)):
                byte_size += len(chunk)
                digest.update(chunk)
                staged.write(chunk)
                budget.consume(len(chunk))
                if byte_size > MAX_IMAGE_BYTES:
                    raise ArchiveSafetyError("image_too_large")
        if byte_size != info.file_size:
            raise ArchiveSafetyError("corrupt_entry")
    except ArchiveSafetyError as error:
        raise _EntryReadError(error.code, byte_size) from None
    except (NotImplementedError, zipfile.BadZipFile, zlib.error):
        raise _EntryReadError("corrupt_entry", byte_size) from None
    return byte_size, digest.hexdigest()


def _next_read_size(byte_size: int, budget: _ArchiveReadBudget) -> int:
    image_remaining = MAX_IMAGE_BYTES - byte_size
    archive_remaining = MAX_ARCHIVE_EXPANDED_BYTES - budget.expanded_bytes
    return min(
        READ_CHUNK_BYTES,
        max(1, image_remaining + 1),
        max(1, archive_remaining + 1),
    )


def _preflight_archive(source: Path) -> None:
    file_size = source.stat().st_size
    if file_size < 22:
        raise ArchiveSafetyError("invalid_archive")
    tail_size = min(file_size, _MAX_EOCD_TAIL_BYTES)
    with source.open("rb") as stream:
        stream.seek(file_size - tail_size)
        tail = stream.read(tail_size)
        eocd_index = _find_eocd(tail)
        if eocd_index is None:
            raise ArchiveSafetyError("invalid_archive")
        eocd_offset = file_size - tail_size + eocd_index
        (
            _signature,
            disk_number,
            directory_disk,
            entries_on_disk,
            total_entries,
            directory_size,
            directory_offset,
            _comment_size,
        ) = struct.unpack_from("<4s4H2LH", tail, eocd_index)
        if (
            disk_number == 0xFFFF
            or directory_disk == 0xFFFF
            or entries_on_disk == 0xFFFF
            or total_entries == 0xFFFF
            or directory_size == 0xFFFFFFFF
        ):
            total_entries, directory_size, directory_offset = (
                _read_zip64_directory_facts(stream, eocd_offset)
            )
        elif disk_number or directory_disk or entries_on_disk != total_entries:
            raise ArchiveSafetyError("multi_disk_archive")
    if total_entries > MAX_ARCHIVE_ENTRIES:
        raise ArchiveSafetyError("entry_limit_exceeded")
    if directory_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise ArchiveSafetyError("central_directory_too_large")
    with source.open("rb") as stream:
        actual_entries = _count_central_directory_entries(
            stream,
            directory_offset=directory_offset,
            directory_size=directory_size,
            eocd_offset=eocd_offset,
        )
    if actual_entries != total_entries:
        raise ArchiveSafetyError("invalid_archive")


def _find_eocd(tail: bytes) -> int | None:
    index = tail.rfind(_EOCD_SIGNATURE)
    while index >= 0:
        if index + 22 <= len(tail):
            comment_size = int.from_bytes(tail[index + 20 : index + 22], "little")
            if index + 22 + comment_size == len(tail):
                return index
        index = tail.rfind(_EOCD_SIGNATURE, 0, index)
    return None


def _read_zip64_directory_facts(
    stream: BinaryIO, eocd_offset: int
) -> tuple[int, int, int]:
    if eocd_offset < 20:
        raise ArchiveSafetyError("invalid_archive")
    stream.seek(eocd_offset - 20)
    locator = stream.read(20)
    if len(locator) != 20 or locator[:4] != _ZIP64_LOCATOR_SIGNATURE:
        raise ArchiveSafetyError("invalid_archive")
    _signature, zip64_disk, zip64_offset, disk_count = struct.unpack("<4sLQL", locator)
    if zip64_disk != 0 or disk_count != 1:
        raise ArchiveSafetyError("multi_disk_archive")
    stream.seek(zip64_offset)
    record = stream.read(56)
    if len(record) != 56 or record[:4] != _ZIP64_EOCD_SIGNATURE:
        raise ArchiveSafetyError("invalid_archive")
    (
        _signature,
        record_size,
        _made_by,
        _required,
        disk_number,
        directory_disk,
        entries_on_disk,
        total_entries,
        directory_size,
        directory_offset,
    ) = struct.unpack("<4sQ2H2L4Q", record)
    if record_size < 44:
        raise ArchiveSafetyError("invalid_archive")
    if disk_number != 0 or directory_disk != 0 or entries_on_disk != total_entries:
        raise ArchiveSafetyError("multi_disk_archive")
    return total_entries, directory_size, directory_offset


def _count_central_directory_entries(
    stream: BinaryIO,
    *,
    directory_offset: int,
    directory_size: int,
    eocd_offset: int,
) -> int:
    directory_end = directory_offset + directory_size
    if directory_offset < 0 or directory_size < 0 or directory_end > eocd_offset:
        raise ArchiveSafetyError("invalid_archive")
    offset = directory_offset
    count = 0
    while offset < directory_end:
        if directory_end - offset < 46:
            raise ArchiveSafetyError("invalid_archive")
        stream.seek(offset)
        header = stream.read(46)
        if len(header) != 46 or header[:4] != _CENTRAL_DIRECTORY_SIGNATURE:
            raise ArchiveSafetyError("invalid_archive")
        filename_size, extra_size, comment_size = struct.unpack_from("<3H", header, 28)
        record_size = 46 + filename_size + extra_size + comment_size
        if record_size > directory_end - offset:
            raise ArchiveSafetyError("invalid_archive")
        count += 1
        if count > MAX_ARCHIVE_ENTRIES:
            raise ArchiveSafetyError("entry_limit_exceeded")
        offset += record_size
    if offset != directory_end:
        raise ArchiveSafetyError("invalid_archive")
    return count
