import io
import json
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

import photo_organizer.catalog.standard_product_package as package_module
from photo_organizer.catalog.mall_draft_export import build_mall_draft_batch
from photo_organizer.catalog.standard_product_package import (
    StandardProductPackageError,
    _matches_media_type,
    build_standard_product_package,
    validate_standard_product_package_manifest,
    write_standard_product_package,
)
from photo_organizer.categorization.contracts import (
    Level1Category,
    Level2Category,
    ProductSourceType,
)
from photo_organizer.schemas.acceptance_receipt import (
    AcceptedImageEvidence,
    SkuMaterialAcceptanceReceipt,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch
from photo_organizer.schemas.standard_product_package import (
    StandardProductPackageManifest,
)

NOW = datetime(2026, 8, 17, 23, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[4]
TEN_ITEM_BATCH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"


def _batch() -> MallProductDraftBatch:
    source_event_id = UUID("00000000-0000-4000-8000-000000000001")
    download_event_id = UUID("50000000-0000-4000-8000-000000000001")
    images = (
        AcceptedImageEvidence(
            asset_id=UUID("30000000-0000-4000-8000-000000000011"),
            evidence_asset_id=UUID("40000000-0000-4000-8000-000000000011"),
            role="original",
            sha256="11" * 32,
            byte_size=101,
            media_type="image/jpeg",
            captured_at=NOW,
            imported_at=NOW,
            storage_ref=("evidence/40000000-0000-4000-8000-000000000011/original"),
        ),
        AcceptedImageEvidence(
            asset_id=UUID("30000000-0000-4000-8000-000000000012"),
            evidence_asset_id=UUID("40000000-0000-4000-8000-000000000012"),
            role="original",
            sha256="12" * 32,
            byte_size=102,
            media_type="image/png",
            captured_at=NOW,
            imported_at=NOW,
            storage_ref=("evidence/40000000-0000-4000-8000-000000000012/original"),
        ),
    )
    receipt = SkuMaterialAcceptanceReceipt(
        schema_version="opc-sku-material-acceptance/2.0",
        acceptance_id=f"sku-material-acceptance:{source_event_id}",
        validated_at=NOW,
        data_classification="synthetic_no_customer_data",
        acceptance_status="material_ready_for_mall_draft_not_listed",
        opc_id="opc-standard-package",
        album_product_id="album-product-0001",
        snapshot_version=1,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        source_event_id=source_event_id,
        download_confirmation_event_id=download_event_id,
        classification_id=UUID("10000000-0000-4000-8000-000000000001"),
        review_id=UUID("20000000-0000-4000-8000-000000000001"),
        classification_version=1,
        confirmed_level_1=Level1Category.NAN_HONG,
        confirmed_level_2=Level2Category.BRACELET,
        confirmed_by="user:00000000-0000-4000-8000-000000000001",
        confirmed_at=NOW,
        images=images,
        download_verified_asset_ids=tuple(image.asset_id for image in images),
        download_ttl_seconds=300,
        claims_not_evaluated=(
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        ),
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )
    return build_mall_draft_batch((receipt,), generated_at=NOW)


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


def test_builds_a_stable_draft_only_package_plan() -> None:
    batch = _batch()

    build = build_standard_product_package(batch)

    batch_digest = batch.batch_id.removeprefix("mall-draft-batch:")
    album_digest = sha256(b"album-product-0001").hexdigest()[:16]
    assert build.manifest.schema_version == "opc-standard-product-package/1.0"
    assert build.manifest.source_batch_id == batch.batch_id
    assert build.manifest.root_directory == f"export-{batch_digest[:16]}"
    assert build.manifest.operation == "create_draft_only"
    assert build.manifest.auto_publish is False
    assert build.manifest.shared_market_opt_in is False
    item = build.manifest.items[0]
    assert item.mall_product_id is None
    assert item.bead_id is None
    assert item.claims_not_evaluated == (
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    )
    assert [plan.package_path for plan in build.image_plans] == [
        f"export-{batch_digest[:16]}/images/album-{album_digest}/main.jpg",
        f"export-{batch_digest[:16]}/images/album-{album_digest}/02.png",
    ]
    assert build.manifest.products_xlsx.package_path == (
        f"export-{batch_digest[:16]}/products.xlsx"
    )
    assert len(build.products_xlsx) == build.manifest.products_xlsx.byte_size


def test_products_workbook_is_macro_free_and_leaves_business_fields_empty() -> None:
    workbook = build_standard_product_package(_batch()).products_xlsx

    with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
        assert archive.namelist() == [
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/sheet1.xml",
        ]
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        serialized = b"".join(archive.read(name) for name in archive.namelist())

    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = sheet.findall("x:sheetData/x:row", namespace)
    headers = [node.text or "" for node in rows[0].findall("x:c/x:is/x:t", namespace)]
    values = [node.text or "" for node in rows[1].findall("x:c/x:is/x:t", namespace)]
    assert headers == [
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
    ]
    assert values[:5] == [
        "album-product-0001",
        "1",
        "first_sale_bead",
        "南红",
        "手串",
    ]
    assert values[8:] == [""] * 8
    assert b"<f" not in serialized
    assert b"vbaProject" not in serialized
    assert b"externalLink" not in serialized
    assert b'Target="http://' not in serialized
    assert b'Target="https://' not in serialized
    assert not any(value.startswith(("http://", "https://")) for value in values)


def test_rejects_a_format_valid_but_semantically_forged_batch_id() -> None:
    batch = _batch().model_copy(update={"batch_id": f"mall-draft-batch:{'a' * 64}"})

    with pytest.raises(StandardProductPackageError) as caught:
        build_standard_product_package(batch)

    assert caught.value.code == "unsafe_batch"


def test_rejects_duplicate_source_identities_even_with_a_current_digest() -> None:
    batch = _batch()
    duplicated = _with_current_batch_id(
        batch.model_copy(update={"items": (batch.items[0], batch.items[0])})
    )

    with pytest.raises(StandardProductPackageError) as caught:
        build_standard_product_package(duplicated)

    assert caught.value.code == "unsafe_batch"


def test_rejects_mixed_download_confirmation_events_within_one_product() -> None:
    batch = _batch()
    item = batch.items[0]
    second_lookup = item.download_lookup[1].model_copy(
        update={"confirmation_event_id": UUID("50000000-0000-4000-8000-000000000099")}
    )
    changed = _with_current_batch_id(
        batch.model_copy(
            update={
                "items": (
                    item.model_copy(
                        update={
                            "download_lookup": (
                                item.download_lookup[0],
                                second_lookup,
                            )
                        }
                    ),
                )
            }
        )
    )

    with pytest.raises(StandardProductPackageError) as caught:
        build_standard_product_package(changed)

    assert caught.value.code == "unsafe_batch"


@pytest.mark.parametrize(
    "boundary",
    (
        "naive_generated_at",
        "auto_publish",
        "wrong_claims",
        "duplicate_asset",
        "duplicate_evidence",
        "duplicate_hash",
        "noncanonical_storage_ref",
    ),
)
def test_rejects_unsafe_batch_mutations_with_one_stable_code(
    boundary: str,
) -> None:
    batch = _batch()
    item = batch.items[0]
    if boundary == "naive_generated_at":
        changed = batch.model_copy(
            update={"generated_at": datetime(2026, 8, 17, 23)}  # noqa: DTZ001
        )
    elif boundary == "auto_publish":
        changed = batch.model_copy(
            update={"items": (item.model_copy(update={"auto_publish": True}),)}
        )
    elif boundary == "wrong_claims":
        changed = batch.model_copy(
            update={
                "items": (
                    item.model_copy(
                        update={
                            "claims_not_evaluated": (
                                "origin",
                                "authenticity",
                                "quality",
                                "warehouse_acceptance",
                            )
                        }
                    ),
                )
            }
        )
    elif boundary in {"duplicate_asset", "duplicate_evidence", "duplicate_hash"}:
        images = list(item.images)
        updates: dict[str, object]
        if boundary == "duplicate_asset":
            updates = {"asset_id": images[0].asset_id}
        elif boundary == "duplicate_evidence":
            updates = {
                "evidence_asset_id": images[0].evidence_asset_id,
                "storage_ref": images[0].storage_ref,
            }
        else:
            updates = {"sha256": images[0].sha256}
        images[1] = images[1].model_copy(update=updates)
        lookups = item.download_lookup
        if boundary == "duplicate_asset":
            lookups = (
                lookups[0],
                lookups[1].model_copy(update={"asset_id": images[0].asset_id}),
            )
        changed = _with_current_batch_id(
            batch.model_copy(
                update={
                    "items": (
                        item.model_copy(
                            update={
                                "images": tuple(images),
                                "download_lookup": lookups,
                            }
                        ),
                    )
                }
            )
        )
    elif boundary == "noncanonical_storage_ref":
        images = list(item.images)
        images[0] = images[0].model_copy(update={"storage_ref": "evidence/../original"})
        changed = _with_current_batch_id(
            batch.model_copy(
                update={"items": (item.model_copy(update={"images": tuple(images)}),)}
            )
        )
    with pytest.raises(StandardProductPackageError) as caught:
        build_standard_product_package(changed)

    assert caught.value.code == "unsafe_batch"


def test_manifest_schema_and_bytes_ignore_generation_time() -> None:
    first_batch = _batch()
    later_batch = first_batch.model_copy(
        update={"generated_at": datetime(2026, 8, 18, tzinfo=UTC)}
    )

    first = build_standard_product_package(first_batch)
    later = build_standard_product_package(later_batch)

    schema = StandardProductPackageManifest.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(first.manifest.model_dump(mode="json"))
    assert first.products_xlsx == later.products_xlsx
    assert first.manifest == later.manifest


def test_writes_a_complete_byte_verified_package(tmp_path: Path) -> None:
    jpeg = b"\xff\xd8\xff\xe0JFIF\x00" + b"\x01" * 16
    png = b"\x89PNG\r\n\x1a\n" + b"\x02" * 16
    batch = _batch()
    item = batch.items[0]
    images = tuple(
        image.model_copy(
            update={
                "sha256": sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        )
        for image, content in zip(item.images, (jpeg, png), strict=True)
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    jpeg_path = tmp_path / "private-input-one.bin"
    png_path = tmp_path / "private-input-two.bin"
    jpeg_path.write_bytes(jpeg)
    png_path.write_bytes(png)
    output = tmp_path / "package.zip"

    manifest = write_standard_product_package(
        batch,
        {
            images[1].asset_id: png_path,
            images[0].asset_id: jpeg_path,
        },
        output,
    )

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [
            manifest.products_xlsx.package_path,
            manifest.items[0].images[0].package_path,
            manifest.items[0].images[1].package_path,
            f"{manifest.root_directory}/manifest.json",
        ]
        parsed = StandardProductPackageManifest.model_validate_json(
            archive.read(f"{manifest.root_directory}/manifest.json")
        )
        assert (
            sha256(archive.read(manifest.products_xlsx.package_path)).hexdigest()
            == manifest.products_xlsx.sha256
        )
        for image in manifest.items[0].images:
            assert sha256(archive.read(image.package_path)).hexdigest() == (
                image.sha256
            )
        assert all(
            not name.startswith("/") and ".." not in Path(name).parts
            for name in archive.namelist()
        )
    assert parsed == manifest


def test_writes_a_complete_ten_item_package_and_workbook(tmp_path: Path) -> None:
    batch = MallProductDraftBatch.model_validate_json(
        TEN_ITEM_BATCH.read_text(encoding="utf-8")
    )
    paths: dict[UUID, Path] = {}
    items = []
    for item in batch.items:
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
    batch = _with_current_batch_id(batch.model_copy(update={"items": tuple(items)}))
    output = tmp_path / "ten-item-package.zip"

    manifest = write_standard_product_package(batch, paths, output)

    assert len(manifest.items) == 10
    assert sum(len(item.images) for item in manifest.items) == 20
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == 22
        workbook = archive.read(manifest.products_xlsx.package_path)
    with zipfile.ZipFile(io.BytesIO(workbook)) as workbook_archive:
        sheet = ElementTree.fromstring(
            workbook_archive.read("xl/worksheets/sheet1.xml")
        )
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    assert len(sheet.findall("x:sheetData/x:row", namespace)) == 11


def test_check_mode_is_read_only_and_detects_stale_package(tmp_path: Path) -> None:
    jpeg = b"\xff\xd8\xff\xe0JFIF\x00" + b"\x03" * 16
    png = b"\x89PNG\r\n\x1a\n" + b"\x04" * 16
    batch = _batch()
    item = batch.items[0]
    images = tuple(
        image.model_copy(
            update={
                "sha256": sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        )
        for image, content in zip(item.images, (jpeg, png), strict=True)
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "one.jpg"
    second = tmp_path / "two.png"
    first.write_bytes(jpeg)
    second.write_bytes(png)
    paths = {images[0].asset_id: first, images[1].asset_id: second}
    output = tmp_path / "package.zip"
    write_standard_product_package(batch, paths, output)
    original = output.read_bytes()
    original_inode = output.stat().st_ino

    checked = write_standard_product_package(batch, paths, output, check=True)

    assert checked.source_batch_id == batch.batch_id
    assert output.read_bytes() == original
    assert output.stat().st_ino == original_inode
    output.write_bytes(b"stale")
    with pytest.raises(StandardProductPackageError) as caught:
        write_standard_product_package(batch, paths, output, check=True)
    assert caught.value.code == "package_stale"
    assert output.read_bytes() == b"stale"


def test_output_must_not_replace_an_input_asset(tmp_path: Path) -> None:
    jpeg = b"\xff\xd8\xff\xe0JFIF\x00" + b"\x05" * 16
    png = b"\x89PNG\r\n\x1a\n" + b"\x06" * 16
    batch = _batch()
    item = batch.items[0]
    images = tuple(
        image.model_copy(
            update={
                "sha256": sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        )
        for image, content in zip(item.images, (jpeg, png), strict=True)
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(jpeg)
    second.write_bytes(png)

    with pytest.raises(StandardProductPackageError) as caught:
        write_standard_product_package(
            batch,
            {images[0].asset_id: first, images[1].asset_id: second},
            first,
        )

    assert caught.value.code == "invalid_arguments"
    assert first.read_bytes() == jpeg


@pytest.mark.parametrize(
    ("header", "media_type", "expected"),
    (
        (b"\xff\xd8\xff\xe0JFIF", "image/jpeg", True),
        (b"\x89PNG\r\n\x1a\nrest", "image/png", True),
        (b"\x00\x00\x00\x14ftypheic\x00\x00\x00\x00heic", "image/heic", True),
        (b"\x00\x00\x00\x14ftypmif1\x00\x00\x00\x00mif1", "image/heif", True),
        (b"\x89PNG\r\n\x1a\nrest", "image/jpeg", False),
        (b"\x00\x00\x00\x14ftypmif1\x00\x00\x00\x00mif1", "image/heic", False),
    ),
)
def test_media_signatures_are_a_closed_set(
    header: bytes,
    media_type: str,
    expected: bool,
) -> None:
    assert _matches_media_type(header, media_type) is expected  # type: ignore[arg-type]


def test_heic_brand_outside_the_ftyp_box_is_not_accepted() -> None:
    forged = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00\x00\x00\x00\x08heic"

    assert _matches_media_type(forged, "image/heic") is False


def test_package_accepts_a_large_ftyp_with_a_heic_major_brand(
    tmp_path: Path,
) -> None:
    heic = b"\x00\x00\x00\x44ftypheic\x00\x00\x00\x00" + b"isom" * 13
    png = b"\x89PNG\r\n\x1a\n" + b"\x0d" * 16
    assert len(heic) == 68
    batch = _batch()
    item = batch.items[0]
    images = (
        item.images[0].model_copy(
            update={
                "sha256": sha256(heic).hexdigest(),
                "byte_size": len(heic),
                "media_type": "image/heic",
            }
        ),
        item.images[1].model_copy(
            update={
                "sha256": sha256(png).hexdigest(),
                "byte_size": len(png),
            }
        ),
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(heic)
    second.write_bytes(png)

    manifest = write_standard_product_package(
        batch,
        {images[0].asset_id: first, images[1].asset_id: second},
        tmp_path / "package.zip",
    )

    assert manifest.items[0].images[0].package_path.endswith("/main.heic")


def test_package_accepts_a_heic_compatible_brand_after_byte_64(
    tmp_path: Path,
) -> None:
    heic = b"\x00\x00\x00\x44ftypisom\x00\x00\x00\x00" + b"isom" * 12 + b"heic"
    png = b"\x89PNG\r\n\x1a\n" + b"\x0e" * 16
    assert len(heic) == 68
    batch = _batch()
    item = batch.items[0]
    images = (
        item.images[0].model_copy(
            update={
                "sha256": sha256(heic).hexdigest(),
                "byte_size": len(heic),
                "media_type": "image/heic",
            }
        ),
        item.images[1].model_copy(
            update={
                "sha256": sha256(png).hexdigest(),
                "byte_size": len(png),
            }
        ),
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(heic)
    second.write_bytes(png)

    manifest = write_standard_product_package(
        batch,
        {images[0].asset_id: first, images[1].asset_id: second},
        tmp_path / "package.zip",
    )

    assert manifest.items[0].images[0].package_path.endswith("/main.heic")


def test_package_rejects_an_ftyp_box_larger_than_the_asset(tmp_path: Path) -> None:
    forged = b"\xff\xff\xff\xfcftypheic\x00\x00\x00\x00"
    png = b"\x89PNG\r\n\x1a\n" + b"\x0f" * 16
    batch = _batch()
    item = batch.items[0]
    images = (
        item.images[0].model_copy(
            update={
                "sha256": sha256(forged).hexdigest(),
                "byte_size": len(forged),
                "media_type": "image/heic",
            }
        ),
        item.images[1].model_copy(
            update={
                "sha256": sha256(png).hexdigest(),
                "byte_size": len(png),
            }
        ),
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(forged)
    second.write_bytes(png)

    with pytest.raises(StandardProductPackageError) as caught:
        write_standard_product_package(
            batch,
            {images[0].asset_id: first, images[1].asset_id: second},
            tmp_path / "package.zip",
        )

    assert caught.value.code == "asset_mismatch"


def test_media_signature_and_digest_are_checked_on_one_byte_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = b"\xff\xd8\xff" + b"a" * 29
    replacement = b"not" + b"b" * 29
    png = b"\x89PNG\r\n\x1a\n" + b"\x10" * 24
    assert len(initial) == len(replacement) == len(png)
    batch = _batch()
    item = batch.items[0]
    images = (
        item.images[0].model_copy(
            update={
                "sha256": sha256(replacement).hexdigest(),
                "byte_size": len(replacement),
            }
        ),
        item.images[1].model_copy(
            update={
                "sha256": sha256(png).hexdigest(),
                "byte_size": len(png),
            }
        ),
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(initial)
    second.write_bytes(png)

    def replace_after_signature(*_args: object) -> bool:
        first.write_bytes(replacement)
        return True

    monkeypatch.setattr(
        package_module,
        "_matches_media_file",
        replace_after_signature,
        raising=False,
    )

    with pytest.raises(StandardProductPackageError) as caught:
        write_standard_product_package(
            batch,
            {images[0].asset_id: first, images[1].asset_id: second},
            tmp_path / "package.zip",
        )

    assert caught.value.code == "asset_mismatch"


def test_size_mismatch_is_rejected_before_opening_an_archive_entry(
    tmp_path: Path,
) -> None:
    plan = build_standard_product_package(_batch()).image_plans[0]
    source = tmp_path / "oversized.bin"
    source.write_bytes(b"x" * (plan.expected_size + 1))

    class ArchiveThatMustNotBeWritten:
        def open(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("archive entry opened before size validation")

    with pytest.raises(StandardProductPackageError) as caught:
        package_module._write_image(  # type: ignore[arg-type]
            ArchiveThatMustNotBeWritten(),
            plan,
            source,
        )

    assert caught.value.code == "asset_mismatch"


def test_asset_mismatch_and_symlink_preserve_existing_output(
    tmp_path: Path,
) -> None:
    jpeg = b"\xff\xd8\xff\xe0JFIF\x00" + b"\x07" * 16
    png = b"\x89PNG\r\n\x1a\n" + b"\x08" * 16
    batch = _batch()
    item = batch.items[0]
    images = tuple(
        image.model_copy(
            update={
                "sha256": sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        )
        for image, content in zip(item.images, (jpeg, png), strict=True)
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(jpeg)
    second.write_bytes(png)
    output = tmp_path / "package.zip"
    output.write_bytes(b"previous-valid-package")
    first.write_bytes(jpeg + b"changed")

    with pytest.raises(StandardProductPackageError) as mismatch:
        write_standard_product_package(
            batch,
            {images[0].asset_id: first, images[1].asset_id: second},
            output,
        )

    assert mismatch.value.code == "asset_mismatch"
    assert output.read_bytes() == b"previous-valid-package"
    first.write_bytes(jpeg)
    symlink = tmp_path / "linked.bin"
    symlink.symlink_to(first)
    with pytest.raises(StandardProductPackageError) as linked:
        write_standard_product_package(
            batch,
            {images[0].asset_id: symlink, images[1].asset_id: second},
            output,
        )
    assert linked.value.code == "asset_mismatch"
    assert output.read_bytes() == b"previous-valid-package"


def test_symlink_loop_is_rejected_without_a_raw_path_error(tmp_path: Path) -> None:
    batch = _batch()
    images = batch.items[0].images
    loop = tmp_path / "loop.bin"
    loop.symlink_to(loop)
    second = tmp_path / "second.bin"
    second.write_bytes(b"unused")

    with pytest.raises(StandardProductPackageError) as caught:
        write_standard_product_package(
            batch,
            {images[0].asset_id: loop, images[1].asset_id: second},
            tmp_path / "package.zip",
        )

    assert caught.value.code == "asset_mismatch"


@pytest.mark.parametrize("replacement", ("fifo", "new_regular_inode"))
def test_path_replacement_after_lstat_is_rejected_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    jpeg = b"\xff\xd8\xff\xe0JFIF\x00" + b"\x0b" * 16
    png = b"\x89PNG\r\n\x1a\n" + b"\x0c" * 16
    batch = _batch()
    item = batch.items[0]
    images = tuple(
        image.model_copy(
            update={
                "sha256": sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        )
        for image, content in zip(item.images, (jpeg, png), strict=True)
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(jpeg)
    second.write_bytes(png)
    output = tmp_path / "package.zip"
    real_open = package_module.os.open
    replaced = False

    def replace_before_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        nonlocal replaced
        if Path(path) == first and not replaced:
            replaced = True
            first.unlink()
            if replacement == "fifo":
                package_module.os.mkfifo(first)
                assert flags & package_module.os.O_NONBLOCK
            else:
                first.write_bytes(jpeg)
        return real_open(path, flags, mode)

    monkeypatch.setattr(package_module.os, "open", replace_before_open)

    with pytest.raises(StandardProductPackageError) as caught:
        write_standard_product_package(
            batch,
            {images[0].asset_id: first, images[1].asset_id: second},
            output,
        )

    assert caught.value.code == "asset_mismatch"
    assert not output.exists()


def test_exact_asset_mapping_and_fixed_zip_metadata(tmp_path: Path) -> None:
    jpeg = b"\xff\xd8\xff\xe0JFIF\x00" + b"\x09" * 16
    png = b"\x89PNG\r\n\x1a\n" + b"\x0a" * 16
    batch = _batch()
    item = batch.items[0]
    images = tuple(
        image.model_copy(
            update={
                "sha256": sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        )
        for image, content in zip(item.images, (jpeg, png), strict=True)
    )
    batch = _with_current_batch_id(
        batch.model_copy(
            update={"items": (item.model_copy(update={"images": images}),)}
        )
    )
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    extra = tmp_path / "extra.bin"
    first.write_bytes(jpeg)
    second.write_bytes(png)
    extra.write_bytes(jpeg)
    output = tmp_path / "package.zip"
    with pytest.raises(StandardProductPackageError) as missing:
        write_standard_product_package(
            batch,
            {images[0].asset_id: first},
            output,
        )
    with pytest.raises(StandardProductPackageError) as extra_mapping:
        write_standard_product_package(
            batch,
            {
                images[0].asset_id: first,
                images[1].asset_id: second,
                UUID("30000000-0000-4000-8000-000000000099"): extra,
            },
            output,
        )
    assert missing.value.code == extra_mapping.value.code == "missing_asset"

    write_standard_product_package(
        batch,
        {images[0].asset_id: first, images[1].asset_id: second},
        output,
    )
    with zipfile.ZipFile(output) as outer:
        for info in outer.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_STORED
            assert (info.external_attr >> 16) & 0o777 == 0o644
        workbook = outer.read(outer.namelist()[0])
    with zipfile.ZipFile(io.BytesIO(workbook)) as inner:
        for info in inner.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_STORED
            assert (info.external_attr >> 16) & 0o777 == 0o644


@pytest.mark.parametrize(
    "mutation",
    (
        "workbook_uses_image_path",
        "image_uses_workbook_path",
        "root_drift",
        "package_identity_drift",
        "storage_ref_drift",
    ),
)
def test_manifest_contract_rejects_path_or_identity_drift(mutation: str) -> None:
    manifest = build_standard_product_package(_batch()).manifest.model_dump(mode="json")
    if mutation == "workbook_uses_image_path":
        manifest["products_xlsx"]["package_path"] = manifest["items"][0]["images"][0][
            "package_path"
        ]
    elif mutation == "image_uses_workbook_path":
        manifest["items"][0]["images"][0]["package_path"] = manifest["products_xlsx"][
            "package_path"
        ]
    elif mutation == "root_drift":
        manifest["root_directory"] = "export-aaaaaaaaaaaaaaaa"
    elif mutation == "package_identity_drift":
        manifest["package_id"] = f"standard-product-package:{'a' * 64}"
    else:
        manifest["items"][0]["images"][0]["storage_ref"] = (
            "evidence/40000000-0000-4000-8000-000000000099/original"
        )

    with pytest.raises(ValidationError):
        StandardProductPackageManifest.model_validate(manifest)


def test_semantic_manifest_verifier_closes_json_schema_identity_gaps() -> None:
    manifest = build_standard_product_package(_batch()).manifest.model_dump(mode="json")
    manifest["root_directory"] = "export-aaaaaaaaaaaaaaaa"

    with pytest.raises(StandardProductPackageError) as caught:
        validate_standard_product_package_manifest(manifest)

    assert caught.value.code == "unsafe_manifest"


@pytest.mark.parametrize(
    "mutation",
    ("opc_id", "classification_version", "image_sha256"),
)
def test_semantic_manifest_verifier_binds_the_source_batch_digest(
    mutation: str,
) -> None:
    manifest = build_standard_product_package(_batch()).manifest.model_dump(mode="json")
    if mutation == "opc_id":
        manifest["opc_id"] = "opc-other"
    elif mutation == "classification_version":
        manifest["items"][0]["classification"]["classification_version"] += 1
    else:
        manifest["items"][0]["images"][0]["sha256"] = "ab" * 32

    with pytest.raises(StandardProductPackageError) as caught:
        validate_standard_product_package_manifest(manifest)

    assert caught.value.code == "unsafe_manifest"


def test_semantic_manifest_verifier_revalidates_nested_model_instances() -> None:
    manifest = build_standard_product_package(_batch()).manifest
    image = (
        manifest.items[0]
        .images[0]
        .model_copy(
            update={
                "storage_ref": (
                    "evidence/40000000-0000-4000-8000-000000000099/original"
                )
            }
        )
    )
    item = manifest.items[0].model_copy(
        update={"images": (image, *manifest.items[0].images[1:])}
    )
    forged = manifest.model_copy(update={"items": (item,)})

    with pytest.raises(StandardProductPackageError) as caught:
        validate_standard_product_package_manifest(forged)

    assert caught.value.code == "unsafe_manifest"


@pytest.mark.parametrize(
    "mutation",
    ("acceptance_group_drift", "draft_request_drift"),
)
def test_rejects_unbound_acceptance_or_draft_identity(mutation: str) -> None:
    batch = _batch()
    item = batch.items[0]
    if mutation == "acceptance_group_drift":
        acceptance_id = "sku-material-acceptance:00000000-0000-4000-8000-000000000099"
        changed_item = item.model_copy(
            update={
                "source_acceptance_id": acceptance_id,
                "draft_request_id": (
                    f"mall-draft-request:{sha256(acceptance_id.encode()).hexdigest()}"
                ),
            }
        )
    else:
        changed_item = item.model_copy(
            update={"draft_request_id": f"mall-draft-request:{'a' * 64}"}
        )
    changed = _with_current_batch_id(
        batch.model_copy(update={"items": (changed_item,)})
    )

    with pytest.raises(StandardProductPackageError) as caught:
        build_standard_product_package(changed)

    assert caught.value.code == "unsafe_batch"
