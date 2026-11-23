from __future__ import annotations

import io
import json
import os
import stat
import struct
import tempfile
import zipfile
from _thread import LockType
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import IO, TYPE_CHECKING, BinaryIO

from pydantic import ValidationError

from photo_organizer.catalog.standard_product_package import (
    StandardProductPackageError,
    _build_products_xlsx,
    _MediaSignatureProbe,
    _row,
    validate_standard_product_package_manifest,
)
from photo_organizer.schemas.standard_product_package import (
    PackagedImageEvidence,
    StandardProductPackageManifest,
)
from photo_organizer.schemas.standard_product_package_acceptance import (
    AcceptedPackageProductSource,
    StandardProductPackageAcceptanceReceipt,
)

if TYPE_CHECKING:
    from photo_organizer.catalog.package_image_streams import (
        PackageImageStreamProvider,
    )
    from photo_organizer.schemas.mall_draft_adapter import MallDraftImportPlan


class StandardProductPackageAcceptanceError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class _VerifiedPackageStreamState:
    _lock: LockType = field(default_factory=Lock)

    def try_open(self) -> bool:
        return self._lock.acquire(blocking=False)

    def close(self) -> None:
        self._lock.release()


@dataclass(frozen=True)
class VerifiedStandardProductPackage:
    _snapshot: BinaryIO = field(repr=False, compare=False)
    manifest: StandardProductPackageManifest
    receipt: StandardProductPackageAcceptanceReceipt
    _stream_state: _VerifiedPackageStreamState = field(
        default_factory=_VerifiedPackageStreamState,
        repr=False,
        compare=False,
    )

    def image_streams(self, plan: MallDraftImportPlan) -> PackageImageStreamProvider:
        from photo_organizer.catalog.package_image_streams import (
            PackageImageStreamProvider,
        )

        return PackageImageStreamProvider(
            self._snapshot,
            self.manifest,
            self.receipt,
            plan,
            self._stream_state,
        )


@contextmanager
def open_verified_standard_product_package(
    path: Path,
) -> Iterator[VerifiedStandardProductPackage]:
    with ExitStack() as resources:
        try:
            snapshot = resources.enter_context(tempfile.TemporaryFile("w+b"))
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise StandardProductPackageAcceptanceError("invalid_package") from None
        try:
            package_sha256, package_byte_size = _copy_package_snapshot(path, snapshot)
            manifest = _verify_snapshot(snapshot)
            receipt = build_standard_product_package_acceptance(
                manifest,
                package_sha256=package_sha256,
                package_byte_size=package_byte_size,
            )
        except StandardProductPackageAcceptanceError:
            raise
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise StandardProductPackageAcceptanceError("invalid_package") from None
        yield VerifiedStandardProductPackage(snapshot, manifest, receipt)


def verify_standard_product_package(
    path: Path,
) -> StandardProductPackageAcceptanceReceipt:
    with open_verified_standard_product_package(path) as verified:
        return verified.receipt


def _copy_package_snapshot(
    path: Path,
    snapshot: BinaryIO,
) -> tuple[str, int]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StandardProductPackageAcceptanceError("invalid_package")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    digest = sha256()
    byte_size = 0
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or opened.st_size <= 0
            or opened.st_size > 4_200_000_000
        ):
            raise StandardProductPackageAcceptanceError("invalid_package")
        while chunk := source.read(128 * 1024):
            byte_size += len(chunk)
            if byte_size > 4_200_000_000:
                raise StandardProductPackageAcceptanceError("invalid_package")
            digest.update(chunk)
            snapshot.write(chunk)
        if byte_size != opened.st_size:
            raise StandardProductPackageAcceptanceError("invalid_package")
    snapshot.seek(0)
    return digest.hexdigest(), byte_size


def _verify_snapshot(snapshot: BinaryIO) -> StandardProductPackageManifest:
    if not zipfile.is_zipfile(snapshot):
        raise StandardProductPackageAcceptanceError("invalid_package")
    snapshot.seek(0)
    try:
        with zipfile.ZipFile(snapshot) as archive:
            infos = archive.infolist()
            if not 4 <= len(infos) <= 42:
                raise StandardProductPackageAcceptanceError("invalid_package")
            _preflight_outer_entries(infos)
            _validate_canonical_zip_layout(
                snapshot,
                archive,
                error_code="invalid_package",
            )
            manifest_infos = [
                info
                for info in infos
                if info.filename.startswith("export-")
                and info.filename.endswith("/manifest.json")
            ]
            if len(manifest_infos) != 1:
                raise StandardProductPackageAcceptanceError("invalid_package")
            manifest_info = manifest_infos[0]
            if manifest_info.file_size > 1_048_576:
                raise StandardProductPackageAcceptanceError("invalid_package")
            manifest = validate_standard_product_package_manifest(
                json.loads(archive.read(manifest_info).decode("utf-8"))
            )
            expected_names = {
                f"{manifest.root_directory}/manifest.json",
                manifest.products_xlsx.package_path,
                *(
                    image.package_path
                    for item in manifest.items
                    for image in item.images
                ),
            }
            if {info.filename for info in infos} != expected_names:
                raise StandardProductPackageAcceptanceError("package_mismatch")
            workbook_info = archive.getinfo(manifest.products_xlsx.package_path)
            if workbook_info.file_size > 2_097_152:
                raise StandardProductPackageAcceptanceError("package_mismatch")
            workbook = archive.read(manifest.products_xlsx.package_path)
            if (
                len(workbook) != manifest.products_xlsx.byte_size
                or sha256(workbook).hexdigest() != manifest.products_xlsx.sha256
            ):
                raise StandardProductPackageAcceptanceError("package_mismatch")
            _validate_products_workbook(workbook, manifest)
            for item in manifest.items:
                for image in item.images:
                    _verify_image(archive, image)
            return manifest
    except StandardProductPackageError:
        raise StandardProductPackageAcceptanceError("package_mismatch") from None
    except RecursionError:
        raise StandardProductPackageAcceptanceError("invalid_package") from None
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ):
        raise StandardProductPackageAcceptanceError("package_mismatch") from None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise StandardProductPackageAcceptanceError("package_mismatch") from None


def _validate_products_workbook(
    content: bytes,
    manifest: StandardProductPackageManifest,
) -> None:
    if len(content) > 2_097_152 or not zipfile.is_zipfile(io.BytesIO(content)):
        raise StandardProductPackageAcceptanceError("package_mismatch")
    expected_names = (
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/worksheets/sheet1.xml",
    )
    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        infos = workbook.infolist()
        _preflight_outer_entries(infos, error_code="package_mismatch")
        _validate_canonical_zip_layout(
            workbook.fp,
            workbook,
            error_code="package_mismatch",
        )
        if tuple(info.filename for info in infos) != expected_names:
            raise StandardProductPackageAcceptanceError("package_mismatch")
        entries = {info.filename: workbook.read(info) for info in infos}
    canonical = _build_products_xlsx(tuple(_row(item) for item in manifest.items))
    with zipfile.ZipFile(io.BytesIO(canonical)) as expected_workbook:
        for name in expected_names:
            if entries[name] != expected_workbook.read(name):
                raise StandardProductPackageAcceptanceError("package_mismatch")


def _preflight_outer_entries(
    infos: list[zipfile.ZipInfo],
    *,
    error_code: str = "invalid_package",
) -> None:
    names = [info.filename for info in infos]
    if len(set(names)) != len(names):
        raise StandardProductPackageAcceptanceError(error_code)
    for info in infos:
        parts = info.filename.split("/")
        mode = info.external_attr >> 16
        if (
            not info.filename
            or info.filename.startswith("/")
            or "\\" in info.filename
            or any(part in {"", ".", ".."} for part in parts)
            or info.is_dir()
            or not stat.S_ISREG(mode)
            or info.flag_bits != 0
            or info.comment
            or info.extra
            or info.compress_type != zipfile.ZIP_STORED
            or info.compress_size != info.file_size
        ):
            raise StandardProductPackageAcceptanceError(error_code)


def _validate_canonical_zip_layout(
    source: IO[bytes] | None,
    archive: zipfile.ZipFile,
    *,
    error_code: str,
) -> None:
    if source is None or archive.comment:
        raise StandardProductPackageAcceptanceError(error_code)
    infos = archive.infolist()
    expected_offset = 0
    for info in infos:
        if info.header_offset != expected_offset:
            raise StandardProductPackageAcceptanceError(error_code)
        source.seek(info.header_offset)
        header = source.read(30)
        if len(header) != 30:
            raise StandardProductPackageAcceptanceError(error_code)
        (
            signature,
            _extract_version,
            flags,
            compression,
            _modified_time,
            _modified_date,
            crc,
            compressed_size,
            file_size,
            name_length,
            extra_length,
        ) = struct.unpack("<4s5H3L2H", header)
        name = source.read(name_length)
        if (
            signature != b"PK\x03\x04"
            or flags != 0
            or compression != zipfile.ZIP_STORED
            or crc != info.CRC
            or compressed_size != info.compress_size
            or file_size != info.file_size
            or name != info.filename.encode("utf-8")
            or extra_length != 0
        ):
            raise StandardProductPackageAcceptanceError(error_code)
        expected_offset = (
            info.header_offset + 30 + name_length + extra_length + info.compress_size
        )
    if expected_offset != archive.start_dir:
        raise StandardProductPackageAcceptanceError(error_code)
    source.seek(0, os.SEEK_END)
    archive_size = source.tell()
    if archive_size < 22:
        raise StandardProductPackageAcceptanceError(error_code)
    source.seek(archive_size - 22)
    end = source.read(22)
    if len(end) != 22:
        raise StandardProductPackageAcceptanceError(error_code)
    (
        signature,
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = struct.unpack("<4s4H2LH", end)
    if (
        signature != b"PK\x05\x06"
        or disk_number != 0
        or central_disk != 0
        or disk_entries != len(infos)
        or total_entries != len(infos)
        or central_offset != archive.start_dir
        or central_offset + central_size != archive_size - 22
        or comment_length != 0
    ):
        raise StandardProductPackageAcceptanceError(error_code)


def _verify_image(
    archive: zipfile.ZipFile,
    image: PackagedImageEvidence,
) -> None:
    info = archive.getinfo(image.package_path)
    if info.file_size != image.byte_size:
        raise StandardProductPackageAcceptanceError("package_mismatch")
    digest = sha256()
    byte_size = 0
    probe = _MediaSignatureProbe(image.media_type, image.byte_size)
    with archive.open(info) as source:
        while chunk := source.read(128 * 1024):
            byte_size += len(chunk)
            digest.update(chunk)
            probe.update(chunk)
    if (
        byte_size != image.byte_size
        or digest.hexdigest() != image.sha256
        or not probe.matches()
    ):
        raise StandardProductPackageAcceptanceError("package_mismatch")


def build_standard_product_package_acceptance(
    manifest: StandardProductPackageManifest,
    *,
    package_sha256: str,
    package_byte_size: int,
) -> StandardProductPackageAcceptanceReceipt:
    try:
        validated = validate_standard_product_package_manifest(manifest)
        return StandardProductPackageAcceptanceReceipt(
            schema_version="opc-standard-product-package-acceptance/1.0",
            acceptance_id=f"standard-package-acceptance:{package_sha256}",
            acceptance_status=("package_ready_for_mall_draft_import_not_listed"),
            package_sha256=package_sha256,
            package_byte_size=package_byte_size,
            package_id=validated.package_id,
            source_batch_id=validated.source_batch_id,
            opc_id=validated.opc_id,
            data_classification=validated.data_classification,
            product_count=len(validated.items),
            image_count=sum(len(item.images) for item in validated.items),
            album_product_ids=tuple(
                item.source_album_product_id for item in validated.items
            ),
            product_sources=tuple(
                AcceptedPackageProductSource(
                    album_product_id=item.source_album_product_id,
                    snapshot_version=item.source_snapshot_version,
                    source_type=item.source_type,
                    confirmation_event_id=(item.source_group_confirmation_event_id),
                )
                for item in validated.items
            ),
            material_authorization=("opc_human_confirmed_for_draft_material_use"),
            operation="validate_only",
            publication_state="draft",
            auto_publish=False,
            shared_market_opt_in=False,
            mall_product_ids=(),
            bead_ids=(),
            claims_not_evaluated=(
                "authenticity",
                "origin",
                "quality",
                "warehouse_acceptance",
            ),
        )
    except (
        AttributeError,
        StandardProductPackageError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise StandardProductPackageAcceptanceError("package_mismatch") from None
