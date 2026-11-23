from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from product_engine.contracts.album import ProductImageSetReadyV1, SourceImageRef
from product_engine.contracts.employee_formal_handoff import (
    EmployeeFormalHandoffReceipt,
    EmployeeFormalHandoffRequest,
    EmployeePromotionAsset,
    EmployeePromotionPlan,
    EmployeePromotionProduct,
    EmployeePromotionProductReceipt,
    canonical_employee_batch_sha256,
    employee_event_id,
    employee_source_task_id,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("33333333-3333-4333-8333-333333333333")
BATCH_ID = "BATCH-20260921035620-3A284AA4"
ITEM_ID = UUID("22222222-2222-4222-8222-222222222222")


def plan() -> EmployeePromotionPlan:
    return EmployeePromotionPlan(
        tenant_id=TENANT_ID,
        batch_id=BATCH_ID,
        products=(
            EmployeePromotionProduct(
                product_code="BCNHDIY914",
                product_form="earring",
                assets=(
                    EmployeePromotionAsset(
                        item_id=ITEM_ID,
                        local_object_key="upload-batches/batch/914-1.jpg",
                        destination_object_key=(
                            "tenants/11111111-1111-4111-8111-111111111111/"
                            "employee-handoffs/BATCH-20260921035620-3A284AA4/"
                            "BCNHDIY914/22222222-2222-4222-8222-222222222222"
                        ),
                        sha256="a" * 64,
                        mime_type="image/jpeg",
                        role="main_candidate",
                        position=0,
                    ),
                ),
            ),
        ),
    )


def event() -> ProductImageSetReadyV1:
    return ProductImageSetReadyV1(
        schema_version=1,
        event_id=employee_event_id(TENANT_ID, BATCH_ID, "BCNHDIY914", "c" * 64),
        occurred_at=datetime(2026, 9, 21, tzinfo=UTC),
        tenant_id=TENANT_ID,
        source_task_id=employee_source_task_id(TENANT_ID, BATCH_ID),
        product_code="BCNHDIY914",
        category="信息不足",
        form="earring",
        classification_confidence=0.0,
        images=[
            SourceImageRef(
                asset_id=ITEM_ID,
                object_key=plan().products[0].assets[0].destination_object_key,
                sha256="a" * 64,
                mime_type="image/jpeg",
                kind="original",
                role="main_candidate",
                quality_confidence=0.0,
            )
        ],
    )


def test_request_accepts_only_request_id() -> None:
    request = EmployeeFormalHandoffRequest.model_validate({"request_id": str(REQUEST_ID)})
    assert request.request_id == REQUEST_ID
    with pytest.raises(ValidationError):
        EmployeeFormalHandoffRequest.model_validate(
            {"request_id": str(REQUEST_ID), "tenant_id": str(TENANT_ID)}
        )


def test_fingerprint_changes_with_asset_order_or_hash() -> None:
    original = plan()
    changed_asset = original.products[0].assets[0].model_copy(update={"sha256": "b" * 64})
    changed_product = original.products[0].model_copy(update={"assets": (changed_asset,)})
    changed = original.model_copy(update={"products": (changed_product,)})
    assert canonical_employee_batch_sha256(original) != canonical_employee_batch_sha256(changed)


def test_deterministic_ids_are_stable_and_content_bound() -> None:
    source_id = employee_source_task_id(TENANT_ID, plan().batch_id)
    assert source_id == employee_source_task_id(TENANT_ID, plan().batch_id)
    first = employee_event_id(TENANT_ID, plan().batch_id, "BCNHDIY914", "a" * 64)
    assert first == employee_event_id(TENANT_ID, plan().batch_id, "BCNHDIY914", "a" * 64)
    assert first != employee_event_id(TENANT_ID, plan().batch_id, "BCNHDIY914", "b" * 64)


@pytest.mark.parametrize(
    "change",
    [
        {"assets": ()},
        {"assets": (plan().products[0].assets[0].model_copy(update={"position": 1}),)},
        {
            "assets": (
                plan().products[0].assets[0],
                plan().products[0].assets[0].model_copy(update={"position": 1}),
            )
        },
    ],
)
def test_product_rejects_empty_non_contiguous_or_duplicate_assets(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EmployeePromotionProduct.model_validate(
            {
                **plan().products[0].model_dump(mode="json"),
                **change,
            }
        )


def test_product_requires_exactly_one_main_candidate() -> None:
    detail = plan().products[0].assets[0].model_copy(update={"role": "gallery"})
    with pytest.raises(ValidationError, match="employee_product_main_image_invalid"):
        EmployeePromotionProduct(
            product_code="BCNHDIY914",
            product_form="earring",
            assets=(detail,),
        )


def test_receipt_factories_preserve_event_identity_and_not_sent_boundary() -> None:
    product = EmployeePromotionProductReceipt.from_event(event(), package_version=2)
    receipt = EmployeeFormalHandoffReceipt.created_from(
        plan(),
        REQUEST_ID,
        (product,),
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
    )
    assert product.event_id == event().event_id
    assert product.package_version == 2
    assert product.asset_count == 1
    assert product.assets[0].asset_id == ITEM_ID
    assert receipt.content_sha256 == canonical_employee_batch_sha256(plan())
    assert receipt.source_type == "employee_import_promotion"
    assert receipt.downstream_status == "not_sent"
