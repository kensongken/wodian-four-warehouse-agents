from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from product_engine.contracts.sales_strategy_v3 import (
    SalesMaterialFactV3,
    SalesStrategyInputV3,
    SalesWarehouseDeliveryV2,
    canonical_sales_strategy_v3_sha256,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
CLASSIFICATION_ID = UUID("22222222-2222-4222-8222-222222222222")
HANDOFF_ID = UUID("33333333-3333-4333-8333-333333333333")


def valid_v3(**updates: object) -> SalesStrategyInputV3:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "product_code": "BCNHDIY914",
        "strategy_input_version": 1,
        "source_material_package_version": 1,
        "source_classification_id": CLASSIFICATION_ID,
        "source_classification_version": 2,
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
            "value": "南红",
            "claim_basis": "merchant_declared",
            "claim_version": 2,
            "warning_text": "商家声明，非鉴定结论",
        },
        "form": {
            "value": "bracelet",
            "evidence": "human_confirmed",
        },
        "unverified_items": (),
        "created_at": datetime(2026, 9, 21, 8, tzinfo=UTC),
        "content_sha256": "0" * 64,
    }
    values.update(updates)
    value = SalesStrategyInputV3.model_validate(values)
    return value.model_copy(update={"content_sha256": canonical_sales_strategy_v3_sha256(value)})


def test_delivery_uses_v3_idempotency_namespace() -> None:
    payload = valid_v3()
    delivery = SalesWarehouseDeliveryV2(
        handoff_id=HANDOFF_ID,
        idempotency_key=f"sales-v3:{TENANT_ID}:{CLASSIFICATION_ID}",
        payload_sha256=payload.content_sha256,
        payload=payload,
    )

    assert delivery.payload.contract_version == "3-sales-strategy-input"


def test_material_warning_must_match_claim_basis() -> None:
    with pytest.raises(ValidationError, match="material warning must match claim basis"):
        SalesMaterialFactV3(
            value="南红",
            claim_basis="merchant_declared",
            claim_version=2,
            warning_text="凭证支持，具体以凭证为准",
        )


def test_information_insufficient_requires_unverified_item() -> None:
    with pytest.raises(ValidationError, match="material warning is required"):
        valid_v3(
            material={
                "value": "信息不足",
                "claim_basis": "information_insufficient",
                "claim_version": 1,
                "warning_text": "材质待人工确认",
            },
            unverified_items=(),
        )


def test_automatic_permissions_remain_false() -> None:
    with pytest.raises(ValidationError, match="automatic permissions must be false"):
        valid_v3(automatic_quote_allowed=True)
