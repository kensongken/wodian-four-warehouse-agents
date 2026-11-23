import io
import json
import os
import stat
import zipfile
from hashlib import sha256
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError

import photo_organizer.catalog.standard_product_package_acceptance as acceptance_module
from photo_organizer.catalog.standard_product_package import (
    build_standard_product_package,
    write_standard_product_package,
)
from photo_organizer.catalog.standard_product_package_acceptance import (
    StandardProductPackageAcceptanceError,
    build_standard_product_package_acceptance,
    open_verified_standard_product_package,
    verify_standard_product_package,
)
from photo_organizer.categorization.contracts import ProductSourceType
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch
from photo_organizer.schemas.standard_product_package_acceptance import (
    StandardProductPackageAcceptanceReceipt,
)

ROOT = Path(__file__).resolve().parents[4]
TEN_ITEM_BATCH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"
PACKAGE_DIGEST = "ab" * 32


def _manifest():
    batch = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    return build_standard_product_package(batch).manifest


def _receipt_payload() -> dict[str, object]:
    receipt = build_standard_product_package_acceptance(
        _manifest(),
        package_sha256=PACKAGE_DIGEST,
        package_byte_size=4096,
    )
    return receipt.model_dump(mode="json")


def _with_current_batch_id(batch: MallProductDraftBatch) -> MallProductDraftBatch:
    canonical = json.dumps(
        {
            "opc_id": batch.opc_id,
            "data_classification": batch.data_classification,
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": ("emit_change_proposal_do_not_overwrite"),
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": [item.model_dump(mode="json") for item in batch.items],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return batch.model_copy(
        update={
            "batch_id": f"mall-draft-batch:{sha256(canonical.encode()).hexdigest()}"
        }
    )


def _write_valid_package(tmp_path: Path, *, product_count: int) -> Path:
    source = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    paths: dict[UUID, Path] = {}
    items = []
    for item in source.items[:product_count]:
        images = []
        for image in item.images:
            content = b"\xff\xd8\xff" + image.asset_id.bytes
            path = tmp_path / f"{image.asset_id}.bin"
            path.write_bytes(content)
            paths[image.asset_id] = path
            images.append(
                image.model_copy(
                    update={
                        "sha256": sha256(content).hexdigest(),
                        "byte_size": len(content),
                        "media_type": "image/jpeg",
                    }
                )
            )
        items.append(item.model_copy(update={"images": tuple(images)}))
    batch = _with_current_batch_id(source.model_copy(update={"items": tuple(items)}))
    output = tmp_path / f"{product_count}-product-package.zip"
    write_standard_product_package(batch, paths, output)
    return output


def _zip_info(
    name: str,
    *,
    mode: int = stat.S_IFREG | 0o644,
    compression: int = zipfile.ZIP_STORED,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = compression
    return info


def _rewrite_package(
    source: Path,
    target: Path,
    *,
    duplicate_first: bool = False,
    extra_name: str | None = None,
    compress_first_image: bool = False,
) -> Path:
    with zipfile.ZipFile(source) as original:
        entries = [(info.filename, original.read(info)) for info in original.infolist()]
    with zipfile.ZipFile(target, "w") as changed:
        for index, (name, content) in enumerate(entries):
            compression = (
                zipfile.ZIP_DEFLATED
                if compress_first_image and index == 1
                else zipfile.ZIP_STORED
            )
            changed.writestr(_zip_info(name, compression=compression), content)
        if duplicate_first:
            changed.writestr(_zip_info(entries[1][0]), entries[1][1])
        if extra_name is not None:
            changed.writestr(_zip_info(extra_name), b"unexpected")
    return target


def _write_package_with_first_image(
    tmp_path: Path,
    *,
    content: bytes,
    media_type: str,
    declared_sha256: str | None = None,
    declared_size: int | None = None,
) -> Path:
    source = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    item = source.items[0]
    second_content = b"\xff\xd8\xff" + item.images[1].asset_id.bytes
    images = (
        item.images[0].model_copy(
            update={
                "sha256": declared_sha256 or sha256(content).hexdigest(),
                "byte_size": declared_size or len(content),
                "media_type": media_type,
            }
        ),
        item.images[1].model_copy(
            update={
                "sha256": sha256(second_content).hexdigest(),
                "byte_size": len(second_content),
                "media_type": "image/jpeg",
            }
        ),
    )
    batch = _with_current_batch_id(
        source.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    build = build_standard_product_package(batch)
    output = tmp_path / f"manual-{media_type.removeprefix('image/')}.zip"
    contents = {
        images[0].asset_id: content,
        images[1].asset_id: second_content,
    }
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            _zip_info(build.manifest.products_xlsx.package_path),
            build.products_xlsx,
        )
        for plan in build.image_plans:
            archive.writestr(_zip_info(plan.package_path), contents[plan.asset_id])
        manifest = json.dumps(
            build.manifest.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        archive.writestr(
            _zip_info(f"{build.manifest.root_directory}/manifest.json"),
            manifest,
        )
    return output


def _replace_workbook(
    package: Path,
    target: Path,
    workbook: bytes,
) -> Path:
    with zipfile.ZipFile(package) as original:
        entries = {info.filename: original.read(info) for info in original.infolist()}
    manifest_name = next(name for name in entries if name.endswith("/manifest.json"))
    manifest = json.loads(entries[manifest_name])
    workbook_name = manifest["products_xlsx"]["package_path"]
    manifest["products_xlsx"]["sha256"] = sha256(workbook).hexdigest()
    manifest["products_xlsx"]["byte_size"] = len(workbook)
    entries[workbook_name] = workbook
    entries[manifest_name] = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    with zipfile.ZipFile(target, "w") as changed:
        for name, content in entries.items():
            changed.writestr(_zip_info(name), content)
    return target


def _mutated_workbook(package: Path, mutation: str) -> bytes:
    with zipfile.ZipFile(package) as outer:
        manifest_name = next(
            name for name in outer.namelist() if name.endswith("/manifest.json")
        )
        manifest = json.loads(outer.read(manifest_name))
        workbook = outer.read(manifest["products_xlsx"]["package_path"])
    with zipfile.ZipFile(io.BytesIO(workbook)) as original:
        entries = {info.filename: original.read(info) for info in original.infolist()}
    sheet_name = "xl/worksheets/sheet1.xml"
    if mutation == "doctype":
        entries[sheet_name] = entries[sheet_name].replace(
            b"<worksheet",
            b'<!DOCTYPE worksheet [<!ENTITY hidden "private">]><worksheet',
            1,
        )
    elif mutation == "unused_namespace":
        entries[sheet_name] = entries[sheet_name].replace(
            b'<worksheet xmlns="',
            b'<worksheet xmlns:hidden="urn:private" xmlns="',
            1,
        )
    elif mutation == "unknown_encoding":
        entries[sheet_name] = entries[sheet_name].replace(
            b"encoding='utf-8'",
            b"encoding='x-private-unknown'",
            1,
        )
    if mutation in {
        "wrong_header",
        "business_value",
        "missing_row",
        "formula",
        "hidden_url_node",
        "nested_text_node",
    }:
        sheet = ElementTree.fromstring(entries[sheet_name])
        namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows = sheet.findall("x:sheetData/x:row", namespace)
        if mutation == "wrong_header":
            rows[0].findall("x:c/x:is/x:t", namespace)[0].text = "Wrong Header"
        elif mutation == "business_value":
            rows[1].findall("x:c/x:is/x:t", namespace)[8].text = "Do not prefill"
        elif mutation == "missing_row":
            sheet_data = sheet.find("x:sheetData", namespace)
            assert sheet_data is not None
            sheet_data.remove(rows[-1])
        elif mutation == "formula":
            ElementTree.SubElement(
                rows[1].findall("x:c", namespace)[0], "f"
            ).text = "1+1"
        elif mutation == "hidden_url_node":
            ElementTree.SubElement(
                sheet, "metadata"
            ).text = "https://operator:secret@private.invalid"
        else:
            ElementTree.SubElement(
                rows[0].findall("x:c/x:is/x:t", namespace)[0],
                "hidden",
            ).text = "https://operator:secret@private.invalid"
        entries[sheet_name] = ElementTree.tostring(
            sheet,
            encoding="utf-8",
            xml_declaration=True,
        )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as changed:
        for index, (name, content) in enumerate(entries.items()):
            compression = (
                zipfile.ZIP_DEFLATED
                if mutation == "compressed" and index == 0
                else zipfile.ZIP_STORED
            )
            info = _zip_info(name, compression=compression)
            if mutation == "entry_comment" and index == 0:
                info.comment = b"private-unaccounted"
            if mutation == "entry_extra" and index == 0:
                info.extra = b"\x01\x00\x00\x00"
            changed.writestr(info, content)
        if mutation == "extra_entry":
            changed.writestr(
                _zip_info("xl/worksheets/sheet2.xml"),
                b"<worksheet/>",
            )
        if mutation == "archive_comment":
            changed.comment = b"private-unaccounted"
    return output.getvalue()


def test_builds_a_deterministic_frozen_acceptance_receipt() -> None:
    manifest = _manifest()

    first = build_standard_product_package_acceptance(
        manifest,
        package_sha256=PACKAGE_DIGEST,
        package_byte_size=4096,
    )
    second = build_standard_product_package_acceptance(
        manifest,
        package_sha256=PACKAGE_DIGEST,
        package_byte_size=4096,
    )

    assert first.schema_version == "opc-standard-product-package-acceptance/1.0"
    assert first.acceptance_id == f"standard-package-acceptance:{PACKAGE_DIGEST}"
    assert first.acceptance_status == "package_ready_for_mall_draft_import_not_listed"
    assert first.product_count == 10
    assert first.image_count == 20
    assert first.album_product_ids == tuple(
        f"album-product-{index:04d}" for index in range(1, 11)
    )
    assert first.operation == "validate_only"
    assert first.material_authorization == "opc_human_confirmed_for_draft_material_use"
    assert tuple(source.album_product_id for source in first.product_sources) == (
        first.album_product_ids
    )
    assert tuple(
        source.model_dump(mode="python") for source in first.product_sources
    ) == tuple(
        {
            "album_product_id": item.source_album_product_id,
            "snapshot_version": item.source_snapshot_version,
            "source_type": item.source_type,
            "confirmation_event_id": item.source_group_confirmation_event_id,
        }
        for item in manifest.items
    )
    assert {source.source_type.value for source in first.product_sources} == {
        "first_sale_bead",
        "legacy_material",
        "redesign_output",
    }
    assert all(source.snapshot_version >= 1 for source in first.product_sources)
    assert first.publication_state == "draft"
    assert first.mall_product_ids == ()
    assert first.bead_ids == ()
    assert first.auto_publish is False
    assert first.shared_market_opt_in is False
    assert first == second
    assert json.dumps(first.model_dump(mode="json"), sort_keys=True) == json.dumps(
        second.model_dump(mode="json"), sort_keys=True
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("acceptance_id", f"standard-package-acceptance:{'cd' * 32}"),
        ("package_id", f"standard-product-package:{'cd' * 32}"),
        ("product_count", 9),
        ("image_count", 19),
        ("album_product_ids", ("album-product-0001",) * 10),
        ("mall_product_ids", ("mall-product-0001",)),
        ("bead_ids", ("bead-0001",)),
        ("auto_publish", True),
        ("shared_market_opt_in", True),
        ("claims_not_evaluated", ("authenticity",)),
        ("material_authorization", "unconfirmed"),
        ("product_sources", ()),
    ],
)
def test_receipt_rejects_mutated_identity_or_business_boundaries(
    field: str,
    value: object,
) -> None:
    payload = _receipt_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        StandardProductPackageAcceptanceReceipt.model_validate(payload)


@pytest.mark.parametrize(
    ("digest", "byte_size"),
    [
        ("AB" * 32, 4096),
        ("ab" * 31, 4096),
        (PACKAGE_DIGEST, 0),
        (PACKAGE_DIGEST, 4_200_000_001),
    ],
)
def test_builder_collapses_invalid_package_identity_to_a_stable_error(
    digest: str,
    byte_size: int,
) -> None:
    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        build_standard_product_package_acceptance(
            _manifest(),
            package_sha256=digest,
            package_byte_size=byte_size,
        )

    assert caught.value.code == "package_mismatch"


@pytest.mark.parametrize(
    "mutation",
    ("source_type", "snapshot_version", "confirmation_event_id"),
)
def test_builder_rejects_a_semantically_stale_manifest_digest(
    mutation: str,
) -> None:
    manifest = _manifest()
    updates = {
        "source_type": ProductSourceType.LEGACY_MATERIAL,
        "snapshot_version": manifest.items[0].source_snapshot_version + 1,
        "confirmation_event_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    }
    fields = {
        "source_type": "source_type",
        "snapshot_version": "source_snapshot_version",
        "confirmation_event_id": "source_group_confirmation_event_id",
    }
    first = manifest.items[0].model_copy(update={fields[mutation]: updates[mutation]})
    forged = manifest.model_copy(update={"items": (first, *manifest.items[1:])})

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        build_standard_product_package_acceptance(
            forged,
            package_sha256=PACKAGE_DIGEST,
            package_byte_size=4096,
        )

    assert caught.value.code == "package_mismatch"


@pytest.mark.parametrize(("product_count", "image_count"), [(1, 2), (10, 20)])
def test_verifies_a_complete_package_from_an_immutable_snapshot(
    tmp_path: Path,
    product_count: int,
    image_count: int,
) -> None:
    package = _write_valid_package(tmp_path, product_count=product_count)
    expected_digest = sha256(package.read_bytes()).hexdigest()

    first = verify_standard_product_package(package)
    second = verify_standard_product_package(package)

    assert first.package_sha256 == expected_digest
    assert first.package_byte_size == package.stat().st_size
    assert first.product_count == product_count
    assert first.image_count == image_count
    assert first == second


def test_exposes_a_verified_manifest_and_receipt_only_during_snapshot_context(
    tmp_path: Path,
) -> None:
    package = _write_valid_package(tmp_path, product_count=1)

    with open_verified_standard_product_package(package) as verified:
        assert verified.manifest.package_id == verified.receipt.package_id
        assert verified.receipt == verify_standard_product_package(package)


def test_verified_package_context_preserves_caller_os_errors(tmp_path: Path) -> None:
    package = _write_valid_package(tmp_path, product_count=1)
    expected = OSError("caller failure")

    with (
        pytest.raises(OSError) as caught,
        open_verified_standard_product_package(package),
    ):
        raise expected

    assert caught.value is expected


def test_rejects_non_zip_and_out_of_range_entry_counts(tmp_path: Path) -> None:
    invalid_packages = []
    raw = tmp_path / "raw.bin"
    raw.write_bytes(b"not a zip")
    invalid_packages.append(raw)
    for count in (3, 43):
        package = tmp_path / f"{count}.zip"
        with zipfile.ZipFile(package, "w") as archive:
            for index in range(count):
                archive.writestr(_zip_info(f"safe-{index}.bin"), b"x")
        invalid_packages.append(package)

    for package in invalid_packages:
        with pytest.raises(StandardProductPackageAcceptanceError) as caught:
            verify_standard_product_package(package)
        assert caught.value.code == "invalid_package"


@pytest.mark.parametrize(
    "mutation",
    ("duplicate", "compressed", "absolute", "traversal", "backslash"),
)
def test_rejects_unsafe_or_non_stored_outer_entries(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = _write_valid_package(tmp_path, product_count=1)
    changed = tmp_path / f"{mutation}.zip"
    if mutation == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            _rewrite_package(source, changed, duplicate_first=True)
    elif mutation == "compressed":
        _rewrite_package(source, changed, compress_first_image=True)
    else:
        unsafe_names = {
            "absolute": "/unexpected.bin",
            "traversal": "export-safe/../unexpected.bin",
            "backslash": r"export-safe\unexpected.bin",
        }
        _rewrite_package(source, changed, extra_name=unsafe_names[mutation])

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(changed)

    assert caught.value.code == "invalid_package"


@pytest.mark.parametrize("boundary", ("directory", "symlink", "encrypted"))
def test_rejects_non_regular_or_encrypted_outer_entries(
    tmp_path: Path,
    boundary: str,
) -> None:
    source = _write_valid_package(tmp_path, product_count=1)
    changed = tmp_path / f"{boundary}.zip"
    if boundary == "encrypted":
        content = bytearray(source.read_bytes())
        central = content.rfind(b"PK\x01\x02")
        assert central >= 0
        flags = int.from_bytes(content[central + 8 : central + 10], "little") | 1
        content[central + 8 : central + 10] = flags.to_bytes(2, "little")
        changed.write_bytes(content)
    else:
        with zipfile.ZipFile(source) as original:
            entries = [
                (info.filename, original.read(info)) for info in original.infolist()
            ]
        with zipfile.ZipFile(changed, "w") as archive:
            for name, content in entries:
                archive.writestr(_zip_info(name), content)
            mode = stat.S_IFDIR | 0o755 if boundary == "directory" else stat.S_IFLNK
            name = "export-extra/directory/" if boundary == "directory" else "link"
            archive.writestr(_zip_info(name, mode=mode), b"target")

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(changed)

    assert caught.value.code == "invalid_package"


@pytest.mark.parametrize(
    "boundary",
    (
        "archive_comment",
        "entry_comment",
        "entry_extra",
        "prefix_bytes",
        "trailing_bytes",
    ),
)
def test_rejects_hidden_or_unaccounted_outer_zip_bytes(
    tmp_path: Path,
    boundary: str,
) -> None:
    source = _write_valid_package(tmp_path, product_count=1)
    changed = tmp_path / f"hidden-{boundary}.zip"
    if boundary == "prefix_bytes":
        changed.write_bytes(b"private-unaccounted" + source.read_bytes())
    elif boundary == "trailing_bytes":
        changed.write_bytes(source.read_bytes() + b"private-unaccounted")
    else:
        with zipfile.ZipFile(source) as original:
            entries = [
                (info.filename, original.read(info)) for info in original.infolist()
            ]
        with zipfile.ZipFile(changed, "w") as archive:
            for index, (name, content) in enumerate(entries):
                info = _zip_info(name)
                if index == 0 and boundary == "entry_comment":
                    info.comment = b"private-unaccounted"
                if index == 0 and boundary == "entry_extra":
                    info.extra = b"\x01\x00\x00\x00"
                archive.writestr(info, content)
            if boundary == "archive_comment":
                archive.comment = b"private-unaccounted"

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(changed)

    assert caught.value.code == "invalid_package"


@pytest.mark.parametrize("boundary", ("missing", "duplicate", "oversized"))
def test_rejects_missing_duplicate_or_oversized_manifest(
    tmp_path: Path,
    boundary: str,
) -> None:
    source = _write_valid_package(tmp_path, product_count=1)
    changed = tmp_path / f"manifest-{boundary}.zip"
    with zipfile.ZipFile(source) as original:
        entries = [(info.filename, original.read(info)) for info in original.infolist()]
    manifest_name, manifest_content = entries[-1]
    with zipfile.ZipFile(changed, "w") as archive:
        for name, content in entries[:-1]:
            archive.writestr(_zip_info(name), content)
        if boundary != "missing":
            content = (
                b"{" + b" " * 1_048_576 + b"}"
                if boundary == "oversized"
                else manifest_content
            )
            archive.writestr(_zip_info(manifest_name), content)
            if boundary == "duplicate":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr(_zip_info(manifest_name), content)

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(changed)

    assert caught.value.code == "invalid_package"


def test_deep_manifest_json_collapses_to_one_safe_package_error(
    tmp_path: Path,
) -> None:
    package = tmp_path / "deep-manifest.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(_zip_info("export-aaaaaaaaaaaaaaaa/products.xlsx"), b"x")
        archive.writestr(_zip_info("export-aaaaaaaaaaaaaaaa/image-1.jpg"), b"x")
        archive.writestr(_zip_info("export-aaaaaaaaaaaaaaaa/image-2.jpg"), b"x")
        archive.writestr(
            _zip_info("export-aaaaaaaaaaaaaaaa/manifest.json"),
            ("[" * 2000 + "0" + "]" * 2000).encode(),
        )

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(package)

    assert caught.value.code == "invalid_package"


@pytest.mark.parametrize("boundary", ("directory", "symlink", "fifo"))
def test_rejects_non_regular_package_sources(
    tmp_path: Path,
    boundary: str,
) -> None:
    source = tmp_path / boundary
    if boundary == "directory":
        source.mkdir()
    elif boundary == "symlink":
        source.symlink_to("missing-package.zip")
    else:
        os.mkfifo(source)

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(source)

    assert caught.value.code == "invalid_package"


def test_rejects_a_package_replaced_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_valid_package(tmp_path, product_count=1)
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(source.read_bytes())
    original_open = acceptance_module.os.open

    def replace_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if Path(path) == source:
            source.unlink()
            replacement.rename(source)
        return original_open(path, flags, mode)

    monkeypatch.setattr(acceptance_module.os, "open", replace_before_open)

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(source)

    assert caught.value.code == "invalid_package"


@pytest.mark.parametrize(
    ("content", "media_type", "declared_sha256", "declared_size"),
    [
        (b"\xff\xd8\xffchanged", "image/jpeg", "cd" * 32, None),
        (b"\xff\xd8\xffsize", "image/jpeg", None, 999),
        (b"\xff\xd8\xffjpeg", "image/png", None, None),
        (b"\x89PNG\r\n\x1a\npng", "image/jpeg", None, None),
        (
            b"\xff\xff\xff\xfcftypheic\x00\x00\x00\x00",
            "image/heic",
            None,
            None,
        ),
    ],
)
def test_rejects_image_digest_size_or_media_mismatch(
    tmp_path: Path,
    content: bytes,
    media_type: str,
    declared_sha256: str | None,
    declared_size: int | None,
) -> None:
    package = _write_package_with_first_image(
        tmp_path,
        content=content,
        media_type=media_type,
        declared_sha256=declared_sha256,
        declared_size=declared_size,
    )

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(package)

    assert caught.value.code == "package_mismatch"


def test_accepts_a_heic_compatible_brand_after_byte_64(tmp_path: Path) -> None:
    heic = b"\x00\x00\x00\x44ftypisom\x00\x00\x00\x00" + b"isom" * 12 + b"heic"
    assert len(heic) == 68
    package = _write_package_with_first_image(
        tmp_path,
        content=heic,
        media_type="image/heic",
    )

    receipt = verify_standard_product_package(package)

    assert receipt.product_count == 1
    assert receipt.image_count == 2


def test_collapses_an_image_crc_failure_to_package_mismatch(tmp_path: Path) -> None:
    package = _write_valid_package(tmp_path, product_count=1)
    with zipfile.ZipFile(package) as archive:
        info = archive.infolist()[1]
    content = bytearray(package.read_bytes())
    name_length = int.from_bytes(
        content[info.header_offset + 26 : info.header_offset + 28], "little"
    )
    extra_length = int.from_bytes(
        content[info.header_offset + 28 : info.header_offset + 30], "little"
    )
    data_offset = info.header_offset + 30 + name_length + extra_length
    content[data_offset] ^= 0x01
    package.write_bytes(content)

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(package)

    assert caught.value.code == "package_mismatch"


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_header",
        "business_value",
        "missing_row",
        "formula",
        "hidden_url_node",
        "nested_text_node",
        "doctype",
        "unused_namespace",
        "unknown_encoding",
        "compressed",
        "extra_entry",
        "archive_comment",
        "entry_comment",
        "entry_extra",
    ),
)
def test_rejects_workbook_structure_or_source_row_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    package = _write_valid_package(tmp_path, product_count=1)
    workbook = _mutated_workbook(package, mutation)
    changed = _replace_workbook(
        package,
        tmp_path / f"workbook-{mutation}.zip",
        workbook,
    )

    with pytest.raises(StandardProductPackageAcceptanceError) as caught:
        verify_standard_product_package(changed)

    assert caught.value.code == "package_mismatch"
