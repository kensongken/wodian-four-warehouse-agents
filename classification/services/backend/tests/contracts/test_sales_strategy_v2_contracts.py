from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from product_engine.contracts.sales_strategy_v2 import (
    SalesStrategyInputV2,
    SalesWarehouseDeliveryV1,
    SalesWarehouseReceiptV1,
    canonical_sales_strategy_v2_sha256,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")
HANDOFF_ID = UUID("77777777-7777-4777-8777-777777777777")


def valid_v2(**updates: object) -> SalesStrategyInputV2:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "product_code": "BCNHDIY914",
        "strategy_input_version": 1,
        "source_material_package_version": 1,
        "source_classification_id": CLASSIFICATION_ID,
        "source_classification_version": 1,
        "source_classification_sha256": "a" * 64,
        "source_image_set_sha256": "b" * 64,
        "images": (
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        "material": {
            "value": "信息不足",
            "evidence": "information_insufficient",
        },
        "form": {"value": "earring", "evidence": "human_confirmed"},
        "unverified_items": ("材质待人工确认",),
        "created_at": datetime(2026, 9, 20, 8, tzinfo=UTC),
        "content_sha256": "0" * 64,
    }
    values.update(updates)
    value = SalesStrategyInputV2.model_validate(values)
    return value.model_copy(update={"content_sha256": canonical_sales_strategy_v2_sha256(value)})


def valid_delivery() -> SalesWarehouseDeliveryV1:
    payload = valid_v2()
    return SalesWarehouseDeliveryV1(
        handoff_id=HANDOFF_ID,
        idempotency_key=f"sales:{payload.tenant_id}:{payload.source_classification_id}",
        payload_sha256=payload.content_sha256,
        payload=payload,
    )


def test_v2_digest_is_stable_and_tracks_authoritative_classification() -> None:
    value = valid_v2()

    assert canonical_sales_strategy_v2_sha256(value) == value.content_sha256
    assert value.source_classification_id == CLASSIFICATION_ID
    assert value.source_classification_version == 1


def test_information_insufficient_requires_material_warning() -> None:
    with pytest.raises(ValidationError, match="material warning is required"):
        valid_v2(unverified_items=())


def test_fixed_automatic_permissions_reject_true() -> None:
    with pytest.raises(ValidationError):
        valid_v2(automatic_quote_allowed=True)


def test_delivery_digest_and_idempotency_are_bound_to_payload() -> None:
    delivery = valid_delivery()

    assert delivery.payload_sha256 == delivery.payload.content_sha256
    assert delivery.idempotency_key == (
        f"sales:{delivery.payload.tenant_id}:{delivery.payload.source_classification_id}"
    )


def test_delivery_rejects_a_mismatched_payload_digest() -> None:
    delivery = valid_delivery()

    with pytest.raises(ValidationError, match="payload digest must match"):
        delivery.model_copy(update={"payload_sha256": "f" * 64}, deep=True).model_validate(
            {
                **delivery.model_dump(mode="json"),
                "payload_sha256": "f" * 64,
            }
        )


def test_receiver_receipt_is_immutable_and_accepted_only() -> None:
    receipt = SalesWarehouseReceiptV1(
        receiver_receipt_id="88888888-8888-4888-8888-888888888888",
        handoff_id=HANDOFF_ID,
        accepted_payload_sha256=valid_delivery().payload_sha256,
        status="accepted",
        accepted_at=datetime(2026, 9, 20, 8, 1, tzinfo=UTC),
    )

    assert receipt.status == "accepted"
    with pytest.raises(ValidationError):
        SalesWarehouseReceiptV1.model_validate(
            {**receipt.model_dump(mode="json"), "status": "pending"}
        )
