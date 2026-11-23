from __future__ import annotations

import io
import json
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final
from uuid import RFC_4122, UUID
from xml.etree import ElementTree

from pydantic import BaseModel, ValidationError

from photo_organizer.schemas.mall_draft_export import (
    MallProductDraftBatch,
    MallProductDraftRequest,
)
from photo_organizer.schemas.standard_product_package import (
    ImageMediaType,
    ImagePackagePath,
    PackagedImageEvidence,
    PackageFileDigest,
    StandardPackageProduct,
    StandardProductPackageManifest,
)

BATCH_ID_PATTERN: Final = re.compile(r"^mall-draft-batch:([0-9a-f]{64})$")
MEDIA_EXTENSIONS: Final[dict[ImageMediaType, str]] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/heif": "heif",
}
EXPECTED_CLAIMS: Final = (
    "authenticity",
    "origin",
    "quality",
    "warehouse_acceptance",
)
EXPECTED_BUSINESS_FIELDS: Final = (
    "title",
    "selling_points",
    "price",
    "inventory",
    "sku",
    "customization",
    "origin_evidence",
)
HEADERS: Final = (
    "Album Product ID",
    "Snapshot Version",
    "Source Type",
    "Level 1 Category",
    "Level 2 Category",
    "Main Image Path",
    "All Image Paths",
    "Confirmed At",
    "Title",
    "Selling Points",
    "Origin Evidence",
    "Price",
    "Inventory",
    "SKU",
    "Customization",
    "Notes",
)
XLSX_MEDIA_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


class StandardProductPackageError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ImageWritePlan:
    asset_id: UUID
    package_path: ImagePackagePath
    expected_sha256: str
    expected_size: int
    media_type: ImageMediaType


@dataclass(frozen=True)
class StandardProductPackageBuild:
    manifest: StandardProductPackageManifest
    products_xlsx: bytes
    image_plans: tuple[ImageWritePlan, ...]


def build_standard_product_package(
    batch: MallProductDraftBatch,
) -> StandardProductPackageBuild:
    try:
        validated = MallProductDraftBatch.model_validate(
            batch.model_dump(mode="python")
        )
        match = BATCH_ID_PATTERN.fullmatch(validated.batch_id)
        if match is None:
            raise StandardProductPackageError("unsafe_batch")
        digest = match.group(1)
        root = f"export-{digest[:16]}"
        items = tuple(
            sorted(
                validated.items,
                key=lambda item: (
                    item.source_album_product_id,
                    item.source_snapshot_version,
                    item.draft_request_id,
                ),
            )
        )
        if validated.batch_id != _batch_id(validated, items):
            raise StandardProductPackageError("unsafe_batch")
        _validate_batch(validated, items)
        image_plans: list[ImageWritePlan] = []
        product_rows: list[tuple[str, ...]] = []
        products: list[StandardPackageProduct] = []
        for item in items:
            product, plans = _product(root, item)
            products.append(product)
            image_plans.extend(plans)
            product_rows.append(_row(product))
        workbook = _build_products_xlsx(tuple(product_rows))
        manifest = StandardProductPackageManifest(
            schema_version="opc-standard-product-package/1.0",
            package_id=(
                "standard-product-package:"
                + sha256(
                    f"opc-standard-product-package/1.0\n{validated.batch_id}".encode()
                ).hexdigest()
            ),
            source_batch_id=validated.batch_id,
            opc_id=validated.opc_id,
            data_classification=validated.data_classification,
            root_directory=root,
            operation="create_draft_only",
            existing_product_policy=("emit_change_proposal_do_not_overwrite"),
            auto_publish=False,
            shared_market_opt_in=False,
            products_xlsx=PackageFileDigest(
                package_path=f"{root}/products.xlsx",
                sha256=sha256(workbook).hexdigest(),
                byte_size=len(workbook),
                media_type=XLSX_MEDIA_TYPE,
            ),
            items=tuple(products),
        )
    except StandardProductPackageError:
        raise
    except (TypeError, ValueError, ValidationError):
        raise StandardProductPackageError("unsafe_batch") from None
    return StandardProductPackageBuild(
        manifest=manifest,
        products_xlsx=workbook,
        image_plans=tuple(image_plans),
    )


def validate_standard_product_package_manifest(
    value: object,
) -> StandardProductPackageManifest:
    try:
        validated = StandardProductPackageManifest.model_validate(
            _manifest_primitive(value)
        )
        if validated.source_batch_id != _manifest_source_batch_id(validated):
            raise StandardProductPackageError("unsafe_manifest")
        return validated
    except StandardProductPackageError:
        raise
    except (TypeError, ValueError, ValidationError):
        raise StandardProductPackageError("unsafe_manifest") from None


def _manifest_primitive(value: object) -> object:
    if isinstance(value, BaseModel):
        return _manifest_primitive(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {key: _manifest_primitive(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_manifest_primitive(child) for child in value]
    return value


def _manifest_source_batch_id(manifest: StandardProductPackageManifest) -> str:
    items: list[dict[str, object]] = []
    for item in manifest.items:
        payload = item.model_dump(mode="json")
        source_images: list[dict[str, object]] = []
        download_lookup: list[dict[str, object]] = []
        for image in payload["images"]:
            source_image = dict(image)
            source_image.pop("package_path")
            confirmation_event_id = source_image.pop("download_confirmation_event_id")
            source_images.append(source_image)
            download_lookup.append(
                {
                    "confirmation_event_id": confirmation_event_id,
                    "asset_id": source_image["asset_id"],
                }
            )
        payload["images"] = source_images
        payload["download_lookup"] = download_lookup
        items.append(payload)
    canonical = json.dumps(
        {
            "opc_id": manifest.opc_id,
            "data_classification": manifest.data_classification,
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": ("emit_change_proposal_do_not_overwrite"),
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": items,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"mall-draft-batch:{sha256(canonical.encode()).hexdigest()}"


def write_standard_product_package(
    batch: MallProductDraftBatch,
    asset_paths: Mapping[UUID, Path],
    output_path: Path,
    *,
    check: bool = False,
) -> StandardProductPackageManifest:
    build = build_standard_product_package(batch)
    expected_asset_ids = {plan.asset_id for plan in build.image_plans}
    if set(asset_paths) != expected_asset_ids:
        raise StandardProductPackageError("missing_asset")
    if any(_paths_collide(output_path, source) for source in asset_paths.values()):
        raise StandardProductPackageError("invalid_arguments")
    temporary_path: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w+b") as output:
            with zipfile.ZipFile(
                output,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                archive.writestr(
                    _zip_info(build.manifest.products_xlsx.package_path),
                    build.products_xlsx,
                )
                for plan in build.image_plans:
                    _write_image(
                        archive,
                        plan,
                        asset_paths[plan.asset_id],
                    )
                manifest_path = f"{build.manifest.root_directory}/manifest.json"
                manifest = (
                    json.dumps(
                        build.manifest.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode()
                archive.writestr(_zip_info(manifest_path), manifest)
            output.flush()
            os.fsync(output.fileno())
        if check:
            if not _same_bytes(temporary_path, output_path):
                raise StandardProductPackageError("package_stale")
            return build.manifest
        os.replace(temporary_path, output_path)
        temporary_path = None
    except StandardProductPackageError:
        raise
    except OSError:
        raise StandardProductPackageError("output_unavailable") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return build.manifest


def _paths_collide(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.abspath(os.fspath(first)) == os.path.abspath(os.fspath(second))


def _same_bytes(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as first_file, second.open("rb") as second_file:
            while first_chunk := first_file.read(128 * 1024):
                if first_chunk != second_file.read(len(first_chunk)):
                    return False
            return second_file.read(1) == b""
    except OSError:
        return False


def _write_image(
    archive: zipfile.ZipFile,
    plan: ImageWritePlan,
    source: Path,
) -> None:
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        raise StandardProductPackageError("missing_asset") from None
    except OSError:
        raise StandardProductPackageError("asset_mismatch") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StandardProductPackageError("asset_mismatch")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(source, flags)
    except FileNotFoundError:
        raise StandardProductPackageError("missing_asset") from None
    except OSError:
        raise StandardProductPackageError("asset_mismatch") from None
    digest = sha256()
    byte_size = 0
    media_probe = _MediaSignatureProbe(plan.media_type, plan.expected_size)
    try:
        with os.fdopen(descriptor, "rb") as input_file:
            opened_metadata = os.fstat(input_file.fileno())
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or (opened_metadata.st_dev, opened_metadata.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or opened_metadata.st_size != plan.expected_size
            ):
                raise StandardProductPackageError("asset_mismatch")
            with archive.open(_zip_info(plan.package_path), "w") as target:
                while chunk := input_file.read(128 * 1024):
                    media_probe.update(chunk)
                    digest.update(chunk)
                    byte_size += len(chunk)
                    target.write(chunk)
    except StandardProductPackageError:
        raise
    except OSError:
        raise StandardProductPackageError("asset_mismatch") from None
    if (
        byte_size != plan.expected_size
        or digest.hexdigest() != plan.expected_sha256
        or not media_probe.matches()
    ):
        raise StandardProductPackageError("asset_mismatch")


def _matches_media_type(header: bytes, media_type: ImageMediaType) -> bool:
    probe = _MediaSignatureProbe(media_type, len(header))
    probe.update(header)
    return probe.matches()


class _MediaSignatureProbe:
    def __init__(self, media_type: ImageMediaType, total_size: int) -> None:
        self._media_type = media_type
        self._total_size = total_size
        self._prefix = bytearray()
        self._box_size: int | None = None
        self._compatible_bytes = 0
        self._compatible_carry = b""
        self._matched = False
        self._invalid = False

    def update(self, chunk: bytes) -> None:
        if self._media_type in {"image/jpeg", "image/png"}:
            limit = 8
            if len(self._prefix) < limit:
                self._prefix.extend(chunk[: limit - len(self._prefix)])
            return
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
        if self._media_type == "image/jpeg":
            return self._prefix.startswith(b"\xff\xd8\xff")
        if self._media_type == "image/png":
            return self._prefix.startswith(b"\x89PNG\r\n\x1a\n")
        return (
            not self._invalid
            and self._box_size is not None
            and self._compatible_bytes == self._box_size - 16
            and not self._compatible_carry
            and self._matched
        )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def _validate_batch(
    batch: MallProductDraftBatch,
    items: tuple[MallProductDraftRequest, ...],
) -> None:
    if (
        batch.operation != "create_draft_only"
        or batch.existing_product_policy != "emit_change_proposal_do_not_overwrite"
        or batch.auto_publish is not False
        or batch.shared_market_opt_in is not False
    ):
        raise StandardProductPackageError("unsafe_batch")
    snapshots = {
        (item.source_album_product_id, item.source_snapshot_version) for item in items
    }
    identity_sets: tuple[Collection[object], ...] = (
        {item.draft_request_id for item in items},
        {item.source_acceptance_id for item in items},
        {item.source_group_confirmation_event_id for item in items},
        {item.source_album_product_id for item in items},
        {item.classification.classification_id for item in items},
        {item.classification.review_id for item in items},
        snapshots,
        {item.download_lookup[0].confirmation_event_id for item in items},
    )
    images = tuple(image for item in items for image in item.images)
    if any(len(identities) != len(items) for identities in identity_sets) or (
        len({image.asset_id for image in images}) != len(images)
        or len({image.evidence_asset_id for image in images}) != len(images)
        or len({image.sha256 for image in images}) != len(images)
    ):
        raise StandardProductPackageError("unsafe_batch")
    for item in items:
        if (
            item.mall_product_id is not None
            or item.bead_id is not None
            or item.publication_state != "draft"
            or item.auto_publish is not False
            or item.shared_market_opt_in is not False
            or item.claims_not_evaluated != EXPECTED_CLAIMS
            or item.required_business_fields != EXPECTED_BUSINESS_FIELDS
            or tuple(lookup.asset_id for lookup in item.download_lookup)
            != tuple(image.asset_id for image in item.images)
            or len({lookup.confirmation_event_id for lookup in item.download_lookup})
            != 1
        ):
            raise StandardProductPackageError("unsafe_batch")


def _batch_id(
    batch: MallProductDraftBatch,
    items: tuple[MallProductDraftRequest, ...],
) -> str:
    canonical = json.dumps(
        {
            "opc_id": batch.opc_id,
            "data_classification": batch.data_classification,
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": ("emit_change_proposal_do_not_overwrite"),
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": [item.model_dump(mode="json") for item in items],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"mall-draft-batch:{sha256(canonical.encode()).hexdigest()}"


def _product(
    root: str,
    item: MallProductDraftRequest,
) -> tuple[StandardPackageProduct, tuple[ImageWritePlan, ...]]:
    product_digest = sha256(item.source_album_product_id.encode()).hexdigest()[:16]
    directory = f"{root}/images/album-{product_digest}"
    download_events = {
        lookup.asset_id: lookup.confirmation_event_id for lookup in item.download_lookup
    }
    images: list[PackagedImageEvidence] = []
    plans: list[ImageWritePlan] = []
    for position, image in enumerate(item.images, start=1):
        extension = MEDIA_EXTENSIONS[image.media_type]
        stem = "main" if position == 1 else f"{position:02d}"
        package_path = f"{directory}/{stem}.{extension}"
        _validate_storage_ref(image.storage_ref, image.evidence_asset_id)
        packaged = PackagedImageEvidence(
            package_path=package_path,
            asset_id=image.asset_id,
            evidence_asset_id=image.evidence_asset_id,
            role=image.role,
            sha256=image.sha256,
            byte_size=image.byte_size,
            media_type=image.media_type,
            captured_at=image.captured_at,
            imported_at=image.imported_at,
            storage_ref=image.storage_ref,
            download_confirmation_event_id=download_events[image.asset_id],
        )
        images.append(packaged)
        plans.append(
            ImageWritePlan(
                asset_id=image.asset_id,
                package_path=package_path,
                expected_sha256=image.sha256,
                expected_size=image.byte_size,
                media_type=image.media_type,
            )
        )
    return (
        StandardPackageProduct(
            draft_request_id=item.draft_request_id,
            source_acceptance_id=item.source_acceptance_id,
            source_group_confirmation_event_id=(
                item.source_group_confirmation_event_id
            ),
            source_album_product_id=item.source_album_product_id,
            source_snapshot_version=item.source_snapshot_version,
            source_type=item.source_type,
            classification=item.classification,
            images=tuple(images),
            required_business_fields=item.required_business_fields,
            claims_not_evaluated=item.claims_not_evaluated,
            mall_product_id=None,
            bead_id=None,
            publication_state="draft",
            auto_publish=False,
            shared_market_opt_in=False,
        ),
        tuple(plans),
    )


def _validate_storage_ref(value: str, evidence_id: UUID) -> None:
    parts = value.split("/")
    if len(parts) != 3 or parts[0] != "evidence" or parts[2] != "original":
        raise StandardProductPackageError("unsafe_batch")
    try:
        parsed = UUID(parts[1])
    except ValueError:
        raise StandardProductPackageError("unsafe_batch") from None
    if (
        str(parsed) != parts[1]
        or parsed.version not in {1, 2, 3, 4, 5}
        or parsed.variant != RFC_4122
        or parsed != evidence_id
    ):
        raise StandardProductPackageError("unsafe_batch")


def _row(product: StandardPackageProduct) -> tuple[str, ...]:
    image_paths = tuple(image.package_path for image in product.images)
    return (
        product.source_album_product_id,
        str(product.source_snapshot_version),
        product.source_type.value,
        product.classification.confirmed_level_1.value,
        product.classification.confirmed_level_2.value,
        image_paths[0],
        ";".join(image_paths),
        product.classification.confirmed_at.isoformat().replace("+00:00", "Z"),
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    )


def _build_products_xlsx(rows: tuple[tuple[str, ...], ...]) -> bytes:
    worksheet = ElementTree.Element(
        "worksheet",
        xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    )
    sheet_data = ElementTree.SubElement(worksheet, "sheetData")
    for row_number, values in enumerate((HEADERS, *rows), start=1):
        row = ElementTree.SubElement(sheet_data, "row", r=str(row_number))
        for column_number, value in enumerate(values, start=1):
            cell = ElementTree.SubElement(
                row,
                "c",
                r=f"{_column_name(column_number)}{row_number}",
                t="inlineStr",
            )
            inline = ElementTree.SubElement(cell, "is")
            text = ElementTree.SubElement(inline, "t")
            text.text = value
    entries = (
        (
            "[Content_Types].xml",
            (
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                b'<Default Extension="xml" ContentType="application/xml"/>'
                b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                b'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                b"</Types>"
            ),
        ),
        (
            "_rels/.rels",
            (
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                b"</Relationships>"
            ),
        ),
        (
            "xl/workbook.xml",
            (
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                b'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                b'<sheets><sheet name="Products" sheetId="1" r:id="rId1"/></sheets>'
                b"</workbook>"
            ),
        ),
        (
            "xl/_rels/workbook.xml.rels",
            (
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                b"</Relationships>"
            ),
        ),
        (
            "xl/worksheets/sheet1.xml",
            ElementTree.tostring(
                worksheet,
                encoding="utf-8",
                xml_declaration=True,
            ),
        ),
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, content)
    return output.getvalue()


def _column_name(number: int) -> str:
    name = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name
