from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

import photo_organizer.catalog.package_image_streams as streams_module
from photo_organizer.catalog.mall_draft_adapter import build_mall_draft_import_plan
from photo_organizer.catalog.package_image_streams import (
    PackageImageStreamError,
    VerifiedImageStream,
)
from photo_organizer.catalog.standard_product_package import (
    write_standard_product_package,
)
from photo_organizer.catalog.standard_product_package_acceptance import (
    open_verified_standard_product_package,
)
from photo_organizer.schemas.mall_draft_adapter import (
    MallDraftImportPlan,
    plan_id_for_body,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch

ROOT = Path(__file__).resolve().parents[4]
TEN_ITEM_BATCH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"


def _with_current_batch_id(batch: MallProductDraftBatch) -> MallProductDraftBatch:
    canonical = json.dumps(
        {
            "opc_id": batch.opc_id,
            "data_classification": batch.data_classification,
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": "emit_change_proposal_do_not_overwrite",
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


def _write_package(
    tmp_path: Path,
    *,
    product_count: int,
) -> tuple[Path, MallProductDraftBatch, dict[UUID, bytes]]:
    source = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    contents: dict[UUID, bytes] = {}
    paths: dict[UUID, Path] = {}
    items = []
    for item in source.items[:product_count]:
        images = []
        for image in item.images:
            content = b"\xff\xd8\xff" + image.asset_id.bytes
            image_path = tmp_path / f"{image.asset_id}.jpg"
            image_path.write_bytes(content)
            contents[image.asset_id] = content
            paths[image.asset_id] = image_path
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
    package = tmp_path / f"{product_count}-item-package.zip"
    write_standard_product_package(batch, paths, package)
    return package, batch, contents


def _assert_code(
    caught: pytest.ExceptionInfo[PackageImageStreamError], code: str
) -> None:
    assert caught.value.code == code
    assert str(caught.value) == code


def _with_recomputed_plan_id(payload: dict[str, object]) -> MallDraftImportPlan:
    body = {key: value for key, value in payload.items() if key != "plan_id"}
    return MallDraftImportPlan.model_validate(
        {"plan_id": plan_id_for_body(body), **body}
    )


def _consume_verified_image(image: VerifiedImageStream) -> bytes:
    return image.read()


def test_streams_a_plan_image_from_the_private_snapshot_without_extraction(
    tmp_path: Path,
) -> None:
    package, batch, contents = _write_package(tmp_path, product_count=1)
    files_created_by_test = sorted(tmp_path.iterdir())

    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        streams = verified.image_streams(plan)
        image_plan = plan.items[0].images[0]
        with streams.open_verified_image(
            draft_request_id=plan.items[0].draft_request_id,
            asset_id=image_plan.asset_id,
        ) as image:
            assert _consume_verified_image(image) == contents[image_plan.asset_id]

    assert sorted(tmp_path.iterdir()) == files_created_by_test


def test_stream_provider_rejects_opens_after_its_snapshot_closes(
    tmp_path: Path,
) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        streams = verified.image_streams(plan)
        item = plan.items[0]

    with (
        pytest.raises(PackageImageStreamError) as caught,
        streams.open_verified_image(
            draft_request_id=item.draft_request_id,
            asset_id=item.images[0].asset_id,
        ),
    ):
        pass

    _assert_code(caught, "snapshot_closed")


@pytest.mark.parametrize("wrong_pair", ("request", "asset", "other_item_asset"))
def test_stream_provider_hides_plan_mismatches(
    tmp_path: Path,
    wrong_pair: str,
) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=2)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        first, second = plan.items
        if wrong_pair == "request":
            draft_request_id = "mall-draft-request:" + "a" * 64
            asset_id = first.images[0].asset_id
        elif wrong_pair == "asset":
            draft_request_id = first.draft_request_id
            asset_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        else:
            draft_request_id = first.draft_request_id
            asset_id = second.images[0].asset_id

        with (
            pytest.raises(PackageImageStreamError) as caught,
            verified.image_streams(plan).open_verified_image(
                draft_request_id=draft_request_id,
                asset_id=asset_id,
            ),
        ):
            pass

    _assert_code(caught, "image_not_in_plan")
    assert "export-" not in str(caught.value)
    assert str(package) not in str(caught.value)


@pytest.mark.parametrize("bytes_to_read", (0, 1))
def test_stream_exit_drains_and_verifies_an_early_consumer(
    tmp_path: Path,
    bytes_to_read: int,
) -> None:
    package, batch, contents = _write_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        item = plan.items[0]
        with verified.image_streams(plan).open_verified_image(
            draft_request_id=item.draft_request_id,
            asset_id=item.images[0].asset_id,
        ) as image:
            assert (
                image.read(bytes_to_read)
                == contents[item.images[0].asset_id][:bytes_to_read]
            )


def test_stream_provider_rejects_a_plan_with_a_mutated_package_hash(
    tmp_path: Path,
) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        mutated = plan.model_copy(update={"package_sha256": "ab" * 32})

        with pytest.raises(PackageImageStreamError) as caught:
            verified.image_streams(mutated)

    _assert_code(caught, "image_not_in_plan")


def test_stream_provider_rejects_a_self_consistent_plan_with_forged_tenant_data(
    tmp_path: Path,
) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        payload = plan.model_dump(mode="json")
        payload["opc_id"] = "opc-forged"
        payload["data_classification"] = "opc_owned_product_material_no_customer_data"
        forged = _with_recomputed_plan_id(payload)

        with pytest.raises(PackageImageStreamError) as caught:
            verified.image_streams(forged)

    _assert_code(caught, "image_not_in_plan")


@pytest.mark.parametrize("reorder", ("items", "images"))
def test_stream_provider_rejects_a_reordered_plan_projection(
    tmp_path: Path,
    reorder: str,
) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=2)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        payload = plan.model_dump(mode="json")
        if reorder == "items":
            payload["items"] = list(reversed(payload["items"]))
        else:
            first = dict(payload["items"][0])
            first["images"] = list(reversed(first["images"]))
            payload["items"] = [first, *payload["items"][1:]]
        reordered = _with_recomputed_plan_id(payload)

        with pytest.raises(PackageImageStreamError) as caught:
            verified.image_streams(reordered)

    _assert_code(caught, "image_not_in_plan")


def test_stream_provider_rejects_concurrent_opens(tmp_path: Path) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        streams = verified.image_streams(plan)
        item = plan.items[0]
        with (
            streams.open_verified_image(
                draft_request_id=item.draft_request_id,
                asset_id=item.images[0].asset_id,
            ),
            pytest.raises(PackageImageStreamError) as caught,
            streams.open_verified_image(
                draft_request_id=item.draft_request_id,
                asset_id=item.images[1].asset_id,
            ),
        ):
            pass

    _assert_code(caught, "stream_in_use")


def test_verified_package_rejects_opens_from_two_of_its_providers(
    tmp_path: Path,
) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        first_provider = verified.image_streams(plan)
        second_provider = verified.image_streams(plan)
        item = plan.items[0]
        with (
            first_provider.open_verified_image(
                draft_request_id=item.draft_request_id,
                asset_id=item.images[0].asset_id,
            ),
            pytest.raises(PackageImageStreamError) as caught,
            second_provider.open_verified_image(
                draft_request_id=item.draft_request_id,
                asset_id=item.images[1].asset_id,
            ),
        ):
            pass

    _assert_code(caught, "stream_in_use")


def test_stream_provider_collapses_read_errors_without_sensitive_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        item = plan.items[0]

        def fail_read(self: object, size: int = -1) -> bytes:
            raise OSError("private stream failure")

        monkeypatch.setattr(streams_module.zipfile.ZipExtFile, "read", fail_read)
        with (
            pytest.raises(PackageImageStreamError) as caught,
            verified.image_streams(plan).open_verified_image(
                draft_request_id=item.draft_request_id,
                asset_id=item.images[0].asset_id,
            ) as image,
        ):
            image.read(1)

    _assert_code(caught, "image_mismatch")
    assert "private stream failure" not in str(caught.value)
    assert "export-" not in str(caught.value)
    assert str(package) not in str(caught.value)


def test_stream_provider_collapses_archive_open_key_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package, batch, _ = _write_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        item = plan.items[0]

        def fail_open(
            self: object, name: object, *args: object, **kwargs: object
        ) -> object:
            raise KeyError("private archive entry")

        monkeypatch.setattr(streams_module.zipfile.ZipFile, "open", fail_open)
        with (
            pytest.raises(PackageImageStreamError) as caught,
            verified.image_streams(plan).open_verified_image(
                draft_request_id=item.draft_request_id,
                asset_id=item.images[0].asset_id,
            ),
        ):
            pass

    _assert_code(caught, "image_mismatch")
    assert "private archive entry" not in str(caught.value)
