import asyncio
import json
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from fakes.mall_draft_gateway import FakeMallDraftGateway
from pydantic import ValidationError

from photo_organizer.catalog.mall_draft_adapter import (
    MallDraftAdapterError,
    build_mall_draft_import_plan,
    execute_mall_draft_import,
    execute_mall_draft_import_with_store,
)
from photo_organizer.catalog.mall_draft_execution_store import (
    LocalMallDraftExecutionStore,
)
from photo_organizer.catalog.mall_draft_export import build_mall_draft_batch
from photo_organizer.catalog.mall_draft_gateway import (
    MallDraftCreateResult,
    MallDraftLookupResult,
)
from photo_organizer.catalog.standard_product_package import (
    build_standard_product_package,
)
from photo_organizer.catalog.standard_product_package_acceptance import (
    build_standard_product_package_acceptance,
)
from photo_organizer.categorization.contracts import ProductSourceType
from photo_organizer.schemas.acceptance_receipt import (
    MaterialDataClassification,
    SkuMaterialAcceptanceReceipt,
)
from photo_organizer.schemas.mall_draft_adapter import (
    MallDraftImportItem,
    MallDraftImportPlan,
    MallDraftImportReceipt,
    MallDraftItemResult,
)
from photo_organizer.schemas.mall_draft_export import (
    MallProductDraftBatch,
    MallProductDraftRequest,
)

ROOT = Path(__file__).resolve().parents[4]
TEN_ITEM_BATCH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"
PACKAGE_DIGEST = "ab" * 32


def _current_batch(items: tuple[MallProductDraftRequest, ...]) -> MallProductDraftBatch:
    source = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    payload = source.model_dump(mode="json")
    payload["items"] = [item.model_dump(mode="json") for item in items]
    canonical = json.dumps(
        {
            "opc_id": payload["opc_id"],
            "data_classification": payload["data_classification"],
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": "emit_change_proposal_do_not_overwrite",
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": payload["items"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload["batch_id"] = f"mall-draft-batch:{sha256(canonical.encode()).hexdigest()}"
    return MallProductDraftBatch.model_validate(payload)


def _acceptance(batch: MallProductDraftBatch):
    build = build_standard_product_package(batch)
    return build, build_standard_product_package_acceptance(
        build.manifest,
        package_sha256=PACKAGE_DIGEST,
        package_byte_size=4096,
    )


def test_builds_deterministic_import_plan() -> None:
    source = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    batch = _current_batch((source.items[0],))
    build, acceptance = _acceptance(batch)

    plan = build_mall_draft_import_plan(batch, build.manifest, acceptance)

    assert plan.schema_version == "opc-mall-draft-import-plan/1.0"
    assert plan.operation == "create_draft_only"
    assert plan.existing_product_policy == "change_proposal_required"
    assert plan.auto_publish is False
    assert plan.shared_market_opt_in is False
    assert plan.items[0].draft_request_id == batch.items[0].draft_request_id
    assert plan.items[0].material_authorization == (
        "opc_human_confirmed_for_draft_material_use"
    )
    assert plan.items[0].mall_product_id is None
    assert plan.items[0].bead_id is None
    assert "storage_ref" not in plan.model_dump_json()


def _inputs():
    batch = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    build, acceptance = _acceptance(batch)
    return batch, build.manifest, acceptance


def test_import_plan_canonical_json_is_stable_for_matching_inputs() -> None:
    batch, manifest, acceptance = _inputs()

    first = build_mall_draft_import_plan(batch, manifest, acceptance)
    second = build_mall_draft_import_plan(batch, manifest, acceptance)

    assert first.plan_id == second.plan_id
    assert first.model_dump_json() == second.model_dump_json()


def _source_receipt(
    item: MallProductDraftRequest,
    *,
    opc_id: str,
    data_classification: MaterialDataClassification,
    generated_at: datetime,
) -> SkuMaterialAcceptanceReceipt:
    return SkuMaterialAcceptanceReceipt(
        schema_version="opc-sku-material-acceptance/2.0",
        acceptance_id=item.source_acceptance_id,
        validated_at=generated_at,
        data_classification=data_classification,
        acceptance_status="material_ready_for_mall_draft_not_listed",
        opc_id=opc_id,
        album_product_id=item.source_album_product_id,
        snapshot_version=item.source_snapshot_version,
        source_type=item.source_type,
        source_event_id=item.source_group_confirmation_event_id,
        download_confirmation_event_id=(item.download_lookup[0].confirmation_event_id),
        classification_id=item.classification.classification_id,
        review_id=item.classification.review_id,
        classification_version=item.classification.classification_version,
        confirmed_level_1=item.classification.confirmed_level_1,
        confirmed_level_2=item.classification.confirmed_level_2,
        confirmed_by=item.classification.confirmed_by,
        confirmed_at=item.classification.confirmed_at,
        images=item.images,
        download_verified_asset_ids=tuple(image.asset_id for image in item.images),
        download_ttl_seconds=300,
        claims_not_evaluated=item.claims_not_evaluated,
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )


def test_import_plan_is_independent_of_reversed_ten_item_input() -> None:
    source = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    receipts = tuple(
        _source_receipt(
            item,
            opc_id=source.opc_id,
            data_classification=source.data_classification,
            generated_at=source.generated_at,
        )
        for item in source.items
    )
    forward = build_mall_draft_batch(receipts, generated_at=source.generated_at)
    reverse = build_mall_draft_batch(
        tuple(reversed(receipts)), generated_at=source.generated_at
    )
    forward_build, forward_acceptance = _acceptance(forward)
    reverse_build, reverse_acceptance = _acceptance(reverse)

    first = build_mall_draft_import_plan(
        forward, forward_build.manifest, forward_acceptance
    )
    second = build_mall_draft_import_plan(
        reverse, reverse_build.manifest, reverse_acceptance
    )

    assert first.plan_id == second.plan_id
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("batch_opc", "batch_package_mismatch"),
        ("batch_id", "batch_package_mismatch"),
        ("album_product", "batch_package_mismatch"),
        ("snapshot", "batch_package_mismatch"),
        ("source_type", "batch_package_mismatch"),
        ("group_event", "batch_package_mismatch"),
        ("classification_id", "batch_package_mismatch"),
        ("classification_version", "batch_package_mismatch"),
        ("review", "batch_package_mismatch"),
        ("confirmed_by", "batch_package_mismatch"),
        ("confirmed_at", "batch_package_mismatch"),
        ("asset", "batch_package_mismatch"),
        ("evidence", "batch_package_mismatch"),
        ("image_order", "batch_package_mismatch"),
        ("image_hash", "batch_package_mismatch"),
        ("image_size", "batch_package_mismatch"),
        ("media_type", "batch_package_mismatch"),
        ("package_id", "package_receipt_mismatch"),
        ("package_hash", "package_receipt_mismatch"),
        ("data_classification", "unsafe_material"),
        ("material_authorization", "unsafe_material"),
        ("risk_list", "unsafe_material"),
        ("mall_product_id", "unsafe_material"),
        ("bead_id", "unsafe_material"),
        ("auto_publish", "unsafe_material"),
        ("shared_market", "unsafe_material"),
    ),
)
def test_rejects_tampered_import_sources_with_stable_codes(
    mutation: str,
    expected_code: str,
) -> None:
    batch, manifest, acceptance = _inputs()
    first = batch.items[0]
    if mutation == "batch_opc":
        batch = batch.model_copy(update={"opc_id": "opc-other"})
    elif mutation == "batch_id":
        batch = batch.model_copy(update={"batch_id": "mall-draft-batch:" + "0" * 64})
    elif mutation == "album_product":
        changed = first.model_copy(
            update={"source_album_product_id": "album-product-9999"}
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "snapshot":
        changed = first.model_copy(update={"source_snapshot_version": 2})
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "source_type":
        changed = first.model_copy(
            update={"source_type": ProductSourceType.LEGACY_MATERIAL}
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "group_event":
        changed = first.model_copy(
            update={
                "source_group_confirmation_event_id": UUID(
                    "90000000-0000-4000-8000-000000000001"
                )
            }
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "classification_id":
        changed = first.model_copy(
            update={
                "classification": first.classification.model_copy(
                    update={
                        "classification_id": UUID(
                            "10000000-0000-4000-8000-000000009999"
                        )
                    }
                )
            }
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "classification_version":
        changed = first.model_copy(
            update={
                "classification": first.classification.model_copy(
                    update={"classification_version": 2}
                )
            }
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "review":
        changed = first.model_copy(
            update={
                "classification": first.classification.model_copy(
                    update={"review_id": UUID("20000000-0000-4000-8000-000000009999")}
                )
            }
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "confirmed_by":
        changed = first.model_copy(
            update={
                "classification": first.classification.model_copy(
                    update={"confirmed_by": "user:changed"}
                )
            }
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "confirmed_at":
        changed = first.model_copy(
            update={
                "classification": first.classification.model_copy(
                    update={
                        "confirmed_at": first.classification.confirmed_at
                        + timedelta(seconds=1)
                    }
                )
            }
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "asset":
        changed_image = first.images[0].model_copy(
            update={"asset_id": UUID("30000000-0000-4000-8000-000000009999")}
        )
        changed = first.model_copy(
            update={"images": (changed_image, *first.images[1:])}
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "evidence":
        changed_image = first.images[0].model_copy(
            update={"evidence_asset_id": UUID("40000000-0000-4000-8000-000000009999")}
        )
        changed = first.model_copy(
            update={"images": (changed_image, *first.images[1:])}
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "image_order":
        changed = first.model_copy(update={"images": tuple(reversed(first.images))})
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "image_hash":
        changed_image = first.images[0].model_copy(update={"sha256": "f0" * 32})
        changed = first.model_copy(
            update={"images": (changed_image, *first.images[1:])}
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "image_size":
        changed_image = first.images[0].model_copy(update={"byte_size": 999})
        changed = first.model_copy(
            update={"images": (changed_image, *first.images[1:])}
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "media_type":
        changed_image = first.images[0].model_copy(update={"media_type": "image/png"})
        changed = first.model_copy(
            update={"images": (changed_image, *first.images[1:])}
        )
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "package_id":
        acceptance = acceptance.model_copy(
            update={"package_id": "standard-product-package:" + "0" * 64}
        )
    elif mutation == "package_hash":
        acceptance = acceptance.model_copy(update={"package_sha256": "cd" * 32})
    elif mutation == "data_classification":
        acceptance = acceptance.model_copy(
            update={
                "data_classification": "opc_owned_product_material_no_customer_data"
            }
        )
    elif mutation == "material_authorization":
        acceptance = acceptance.model_copy(update={"material_authorization": "no"})
    elif mutation == "risk_list":
        changed = first.model_copy(update={"claims_not_evaluated": ("origin",)})
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "mall_product_id":
        changed = first.model_copy(update={"mall_product_id": "mall-product-1"})
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "bead_id":
        changed = first.model_copy(update={"bead_id": "bead-1"})
        batch = batch.model_copy(update={"items": (changed, *batch.items[1:])})
    elif mutation == "auto_publish":
        batch = batch.model_copy(update={"auto_publish": True})
    else:
        batch = batch.model_copy(update={"shared_market_opt_in": True})

    with pytest.raises(MallDraftAdapterError) as raised:
        build_mall_draft_import_plan(batch, manifest, acceptance)

    assert raised.value.code == expected_code
    assert str(raised.value) == expected_code


def test_import_plan_model_rejects_a_stale_plan_id() -> None:
    batch, manifest, acceptance = _inputs()
    plan = build_mall_draft_import_plan(batch, manifest, acceptance)
    stale = plan.model_dump(mode="json")
    stale["plan_id"] = "mall-draft-import-plan:" + "0" * 64

    with pytest.raises(ValidationError):
        MallDraftImportPlan.model_validate(stale)


def test_item_result_rejects_mall_id_for_non_created_status() -> None:
    with pytest.raises(ValidationError):
        MallDraftItemResult(
            draft_request_id="mall-draft-request-1",
            attempt_generation=1,
            status="change_proposal_required",
            mall_product_id="mall-product-1",
            error_code=None,
        )


@pytest.mark.parametrize(
    ("status", "mall_product_id", "error_code"),
    (
        ("prepared", None, "gateway_unavailable"),
        ("created", None, None),
        ("created", "mall-product-1", "gateway_protocol_error"),
        ("change_proposal_required", None, "gateway_protocol_error"),
        ("retryable_failed", None, None),
        ("retryable_failed", None, "gateway_protocol_error"),
        ("permanently_blocked", None, None),
        ("permanently_blocked", None, "gateway_unavailable"),
    ),
)
def test_item_result_rejects_every_invalid_status_identity_error_combination(
    status: str,
    mall_product_id: str | None,
    error_code: str | None,
) -> None:
    """Catches receipts whose status contradicts their identity or error fields."""
    with pytest.raises(ValidationError):
        MallDraftItemResult.model_validate(
            {
                "draft_request_id": "mall-draft-request-1",
                "attempt_generation": 1,
                "status": status,
                "mall_product_id": mall_product_id,
                "error_code": error_code,
            }
        )


@pytest.mark.parametrize("execution_status", ("prepared", "in_progress"))
def test_import_receipt_requires_completed_for_an_entirely_terminal_result_set(
    execution_status: str,
) -> None:
    """Catches a terminal receipt mislabeled as nonterminal and then silently skipped."""
    terminal = MallDraftItemResult(
        draft_request_id="mall-draft-request-1",
        attempt_generation=1,
        status="created",
        mall_product_id="mall-product-1",
        error_code=None,
    )

    with pytest.raises(ValidationError):
        MallDraftImportReceipt.model_validate(
            {
                "schema_version": "opc-mall-draft-import-receipt/1.0",
                "plan_id": "mall-draft-import-plan:" + "a" * 64,
                "opc_id": "opc-mall-draft-adapter",
                "execution_status": execution_status,
                "item_count": 1,
                "results": [terminal.model_dump(mode="json")],
            }
        )


def test_import_receipt_rejects_unsorted_or_incomplete_results() -> None:
    first = MallDraftItemResult(
        draft_request_id="mall-draft-request-b",
        attempt_generation=1,
        status="created",
        mall_product_id="mall-product-1",
        error_code=None,
    )
    second = MallDraftItemResult(
        draft_request_id="mall-draft-request-a",
        attempt_generation=1,
        status="prepared",
        mall_product_id=None,
        error_code=None,
    )

    with pytest.raises(ValidationError):
        MallDraftImportReceipt(
            schema_version="opc-mall-draft-import-receipt/1.0",
            plan_id="mall-draft-import-plan:" + "a" * 64,
            opc_id="opc-mall-draft-adapter",
            execution_status="completed",
            item_count=1,
            results=(first, second),
        )


def _write_verified_package(
    tmp_path: Path,
    *,
    product_count: int,
) -> tuple[Path, MallProductDraftBatch, dict[UUID, bytes]]:
    from photo_organizer.catalog.standard_product_package import (
        write_standard_product_package,
    )

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
    batch = _current_batch(tuple(items))
    package = tmp_path / "mall-draft-package.zip"
    write_standard_product_package(batch, paths, package)
    return package, batch, contents


@pytest.mark.asyncio
async def test_dry_run_prepares_every_item_without_gateway_or_stream_access(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=2)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway()

        receipt = await execute_mall_draft_import(
            plan,
            verified.image_streams(plan),
            gateway,
            dry_run=True,
        )

    assert receipt.execution_status == "prepared"
    assert [
        (result.status, result.attempt_generation) for result in receipt.results
    ] == [
        ("prepared", 1),
        ("prepared", 1),
    ]
    assert all(result.mall_product_id is None for result in receipt.results)
    assert gateway.lookup_call_count == 0
    assert gateway.create_call_count == 0
    assert gateway.consumed_image_bytes == 0


@pytest.mark.asyncio
async def test_creates_a_new_draft_after_lookup_and_consumes_verified_images(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, contents = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        item = plan.items[0]
        gateway = FakeMallDraftGateway(
            lookup_result=MallDraftLookupResult(status="not_found"),
            create_result=MallDraftCreateResult(
                opc_id=plan.opc_id,
                draft_request_id=item.draft_request_id,
                source_album_product_id=item.source_album_product_id,
                mall_product_id="mall-draft-0001",
                publication_state="draft",
            ),
        )

        receipt = await execute_mall_draft_import(
            plan,
            verified.image_streams(plan),
            gateway,
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "created"
    assert receipt.results[0].mall_product_id == "mall-draft-0001"
    assert gateway.lookup_call_count == 1
    assert gateway.create_call_count == 1
    assert gateway.consumed_image_bytes == sum(
        len(value) for value in contents.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_status", ("existing_draft", "existing_published"))
async def test_existing_product_requires_change_proposal_without_create(
    tmp_path: Path,
    lookup_status: str,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(
            lookup_result=MallDraftLookupResult.model_validate(
                {"status": lookup_status}
            )
        )

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "change_proposal_required"
    assert receipt.results[0].mall_product_id is None
    assert receipt.results[0].error_code is None
    assert gateway.lookup_call_count == 1
    assert gateway.create_call_count == 0


@pytest.mark.asyncio
async def test_ambiguous_lookup_permanently_blocks_with_a_safe_code(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(
            lookup_result=MallDraftLookupResult(status="ambiguous")
        )

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].error_code == "gateway_protocol_error"
    assert gateway.create_call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("lookup", "create"))
async def test_temporary_gateway_exception_is_retryable_without_exception_text(
    tmp_path: Path,
    operation: str,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(fail_operation=operation)

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "in_progress"
    assert receipt.results[0].status == "retryable_failed"
    assert receipt.results[0].error_code == "gateway_unavailable"
    assert "private gateway failure" not in receipt.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ("opc", "request", "album", "publication"))
async def test_mismatched_create_response_permanently_blocks(
    tmp_path: Path,
    mismatch: str,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        item = plan.items[0]
        response: object = MallDraftCreateResult(
            opc_id="opc-other" if mismatch == "opc" else plan.opc_id,
            draft_request_id=(
                "mall-draft-request-other"
                if mismatch == "request"
                else item.draft_request_id
            ),
            source_album_product_id=(
                "album-product-other"
                if mismatch == "album"
                else item.source_album_product_id
            ),
            mall_product_id="mall-draft-0001",
            publication_state="draft",
        )
        if mismatch == "publication":
            response = _NonDraftCreateResult(plan.opc_id, item)
        gateway = FakeMallDraftGateway(create_result=response)

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].error_code == "gateway_protocol_error"
    assert receipt.results[0].mall_product_id is None


class _NonDraftCreateResult:
    def __init__(self, opc_id: str, item: MallDraftImportItem) -> None:
        self.opc_id = opc_id
        self.draft_request_id = item.draft_request_id
        self.source_album_product_id = item.source_album_product_id
        self.mall_product_id = "mall-draft-0001"
        self.publication_state = "published"


@pytest.mark.asyncio
async def test_retry_reexecutes_only_previous_retryable_items(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=10)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        failed_album_ids = {
            plan.items[1].source_album_product_id,
            plan.items[6].source_album_product_id,
        }
        gateway = FakeMallDraftGateway(failing_lookup_album_ids=failed_album_ids)
        first = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

        assert [result.status for result in first.results].count("created") == 8
        assert [result.status for result in first.results].count(
            "retryable_failed"
        ) == 2

        gateway.reset_calls()
        gateway.failing_lookup_album_ids.clear()
        second = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway, prior_receipt=first
        )

        assert second.execution_status == "completed"
        assert gateway.lookup_call_count == 2
        assert gateway.create_call_count == 2
        retry_generations = {
            result.draft_request_id: result.attempt_generation
            for result in second.results
        }
        assert retry_generations[plan.items[1].draft_request_id] == 2
        assert retry_generations[plan.items[6].draft_request_id] == 2

        gateway.reset_calls()
        third = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway, prior_receipt=second
        )

    assert third == second
    assert gateway.lookup_call_count == 0
    assert gateway.create_call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_mode", ("stream_failure", "overlapping_opens"))
async def test_stream_protocol_failures_never_create_a_success(
    tmp_path: Path,
    gateway_mode: str,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(mode=gateway_mode)

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].mall_product_id is None
    assert receipt.results[0].error_code == "gateway_protocol_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gateway_mode",
    ("zero_open", "partial_open", "repeated_open", "out_of_order"),
)
async def test_gateway_must_consume_each_image_exactly_once_in_order(
    tmp_path: Path,
    gateway_mode: str,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(mode=gateway_mode)

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].mall_product_id is None
    assert receipt.results[0].error_code == "gateway_protocol_error"


@pytest.mark.asyncio
async def test_gateway_cannot_return_while_a_verified_stream_is_open(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(mode="return_with_open_stream")
        streams = verified.image_streams(plan)

        receipt = await execute_mall_draft_import(plan, streams, gateway)
        with streams.open_verified_image(
            draft_request_id=plan.items[0].draft_request_id,
            asset_id=plan.items[0].images[0].asset_id,
        ) as image:
            image.read()

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].error_code == "gateway_protocol_error"


@pytest.mark.asyncio
async def test_retained_source_handle_is_invalid_after_gateway_returns(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(mode="retain_handle")

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.results[0].status == "created"
    assert callable(gateway.retained_open)
    from photo_organizer.catalog.mall_draft_adapter import _ImageSourceProtocolError

    with pytest.raises(_ImageSourceProtocolError):
        gateway.retained_open()


@pytest.mark.asyncio
async def test_delayed_retained_handle_is_revoked_without_mutating_receipt(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(mode="retain_handle_after_return")

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert gateway.post_return_attempted is False
    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "created"
    assert gateway.post_return_task is not None
    await gateway.post_return_task
    assert gateway.post_return_attempted is True
    assert receipt.results[0].status == "created"


@pytest.mark.asyncio
async def test_malformed_create_response_is_a_safe_protocol_block(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(create_result=object())

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].error_code == "gateway_protocol_error"


@pytest.mark.asyncio
async def test_malformed_lookup_response_is_a_safe_protocol_block(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway(lookup_result=object())

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].error_code == "gateway_protocol_error"


class _InvalidLookupStatusPretendingToBeNotFound:
    def __hash__(self) -> int:
        return hash("not_found")

    def __eq__(self, other: object) -> bool:
        return other == "not_found"


@pytest.mark.asyncio
async def test_mutated_lookup_model_is_revalidated_before_create(
    tmp_path: Path,
) -> None:
    """Catches model-instance trust allowing an invalid lookup status into create."""
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        mutated = MallDraftLookupResult(status="not_found").model_copy(
            update={"status": _InvalidLookupStatusPretendingToBeNotFound()}
        )
        gateway = FakeMallDraftGateway(lookup_result=mutated)

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].error_code == "gateway_protocol_error"
    assert gateway.create_call_count == 0


@pytest.mark.asyncio
async def test_mutated_create_model_with_unsafe_id_settles_as_protocol_error(
    tmp_path: Path,
) -> None:
    """Catches an unsafe model-copy ID escaping after external creation."""
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    unsafe_id = "https://private.invalid/mall-product"
    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        item = plan.items[0]
        mutated = MallDraftCreateResult(
            opc_id=plan.opc_id,
            draft_request_id=item.draft_request_id,
            source_album_product_id=item.source_album_product_id,
            mall_product_id="mall-draft-valid",
            publication_state="draft",
        ).model_copy(update={"mall_product_id": unsafe_id})
        gateway = FakeMallDraftGateway(create_result=mutated)

        receipt = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway
        )

    assert receipt.execution_status == "completed"
    assert receipt.results[0].status == "permanently_blocked"
    assert receipt.results[0].mall_product_id is None
    assert receipt.results[0].error_code == "gateway_protocol_error"
    assert unsafe_id not in receipt.model_dump_json()


@pytest.mark.asyncio
async def test_stale_plan_or_prior_receipt_is_rejected_before_gateway_access(
    tmp_path: Path,
) -> None:
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway()
        stale_plan = plan.model_copy(
            update={"plan_id": "mall-draft-import-plan:" + "0" * 64}
        )

        with pytest.raises(MallDraftAdapterError) as stale_plan_error:
            await execute_mall_draft_import(
                stale_plan, verified.image_streams(plan), gateway
            )
        assert stale_plan_error.value.code == "invalid_input"
        assert gateway.lookup_call_count == 0

        prepared = await execute_mall_draft_import(
            plan, verified.image_streams(plan), gateway, dry_run=True
        )
        stale_prior = prepared.model_copy(
            update={"plan_id": "mall-draft-import-plan:" + "1" * 64}
        )
        with pytest.raises(MallDraftAdapterError) as stale_prior_error:
            await execute_mall_draft_import(
                plan, verified.image_streams(plan), gateway, prior_receipt=stale_prior
            )

    assert stale_prior_error.value.code == "resume_state_conflict"
    assert gateway.lookup_call_count == 0


class _BarrierGateway(FakeMallDraftGateway):
    def __init__(self) -> None:
        super().__init__()
        self.create_started = asyncio.Event()
        self.release_create = asyncio.Event()

    async def create_draft(self, **kwargs: object) -> MallDraftCreateResult:
        self.create_started.set()
        await self.release_create.wait()
        return await super().create_draft(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_two_local_runners_share_one_create_and_terminal_receipt(
    tmp_path: Path,
) -> None:
    """Catches concurrent runners calling the gateway before a receipt settles."""
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = _BarrierGateway()
        store = LocalMallDraftExecutionStore(tmp_path / "receipt.json")
        first = asyncio.create_task(
            execute_mall_draft_import_with_store(
                plan, verified.image_streams(plan), gateway, store
            )
        )
        await gateway.create_started.wait()
        second = asyncio.create_task(
            execute_mall_draft_import_with_store(
                plan, verified.image_streams(plan), gateway, store
            )
        )
        gateway.release_create.set()
        first_receipt, second_receipt = await asyncio.gather(first, second)

    assert gateway.create_call_count == 1
    assert first_receipt.results[0].mall_product_id == "mall-draft-default"
    assert second_receipt.results[0].mall_product_id == "mall-draft-default"


@pytest.mark.asyncio
async def test_cancelling_a_blocked_gateway_call_releases_the_execution_locks(
    tmp_path: Path,
) -> None:
    """Catches cancellation leaving the receipt lock held for future local runs."""
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = _BarrierGateway()
        store = LocalMallDraftExecutionStore(tmp_path / "receipt.json")
        cancelled = asyncio.create_task(
            execute_mall_draft_import_with_store(
                plan, verified.image_streams(plan), gateway, store
            )
        )
        await gateway.create_started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        gateway.release_create.set()
        receipt = await asyncio.wait_for(
            execute_mall_draft_import_with_store(
                plan, verified.image_streams(plan), gateway, store
            ),
            timeout=1,
        )

    assert receipt.results[0].mall_product_id == "mall-draft-default"
    assert gateway.create_call_count == 1


@pytest.mark.asyncio
async def test_store_replay_skips_a_prior_terminal_result(tmp_path: Path) -> None:
    """Catches a settled terminal result being sent to the gateway again."""
    from photo_organizer.catalog.standard_product_package_acceptance import (
        open_verified_standard_product_package,
    )

    package, batch, _ = _write_verified_package(tmp_path, product_count=1)
    with open_verified_standard_product_package(package) as verified:
        plan = build_mall_draft_import_plan(batch, verified.manifest, verified.receipt)
        gateway = FakeMallDraftGateway()
        store = LocalMallDraftExecutionStore(tmp_path / "receipt.json")
        first = await execute_mall_draft_import_with_store(
            plan, verified.image_streams(plan), gateway, store
        )
        gateway.reset_calls()
        replay = await execute_mall_draft_import_with_store(
            plan, verified.image_streams(plan), gateway, store
        )

    assert replay == first
    assert gateway.lookup_call_count == 0
    assert gateway.create_call_count == 0
