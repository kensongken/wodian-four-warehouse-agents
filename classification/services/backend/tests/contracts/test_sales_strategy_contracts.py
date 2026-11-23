from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from product_engine.contracts.sales_strategy import (
    SalesStrategyInputUpdatedV1,
    SalesStrategyInputV1,
    canonical_sales_strategy_sha256,
)


def strategy_payload() -> dict[str, object]:
    return {
        "contract_version": "1-sales-strategy-input",
        "tenant_id": "22222222-2222-4222-8222-222222222222",
        "product_code": "SYNTHETIC-001",
        "strategy_input_version": 1,
        "product_data_version": 1,
        "source_album_event_id": "11111111-1111-4111-8111-111111111111",
        "source_material_package_version": 1,
        "source_image_set_sha256": "a" * 64,
        "images": [
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "sha256": "b" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            }
        ],
        "material": {"value": "绿松石", "confirmation_status": "candidate"},
        "form": {"value": "手串", "confirmation_status": "candidate"},
        "inventory_status": "pending",
        "visual_observations": ["蓝绿色圆珠"],
        "selling_point_candidates": ["颜色协调"],
        "price_status": "pending",
        "service_rule_summaries": [],
        "unverified_items": ["真伪未验证", "产地未验证"],
        "forbidden_expressions": ["官方保真", "保证升值"],
        "status": "preview_only",
        "created_at": "2026-08-31T08:00:00Z",
        "content_sha256": "c" * 64,
        "automatic_customer_contact_allowed": False,
        "automatic_publication_allowed": False,
        "automatic_wechat_send_allowed": False,
        "automatic_quote_allowed": False,
    }


def test_phase_one_contract_is_preview_only_and_sales_safe() -> None:
    value = SalesStrategyInputV1.model_validate(strategy_payload())
    assert value.status == "preview_only"
    assert value.tenant_id == UUID("22222222-2222-4222-8222-222222222222")
    assert value.created_at == datetime(2026, 8, 31, 8, tzinfo=UTC)
    assert value.automatic_customer_contact_allowed is False
    assert value.automatic_publication_allowed is False
    assert value.automatic_wechat_send_allowed is False
    assert value.automatic_quote_allowed is False


def test_phase_one_rejects_sales_ready_and_extra_fields() -> None:
    payload = strategy_payload()
    payload["status"] = "sales_ready"
    with pytest.raises(ValidationError):
        SalesStrategyInputV1.model_validate(payload)
    with pytest.raises(ValidationError):
        SalesStrategyInputV1.model_validate({**strategy_payload(), "customer_id": "secret"})


def test_canonical_sha_ignores_only_content_sha_field() -> None:
    payload = strategy_payload()
    value = SalesStrategyInputV1.model_validate(payload)
    changed_digest = value.model_copy(update={"content_sha256": "d" * 64})
    assert canonical_sales_strategy_sha256(value) == canonical_sales_strategy_sha256(changed_digest)
    changed_content = value.model_copy(update={"visual_observations": ["不同特征"]})
    assert canonical_sales_strategy_sha256(value) != canonical_sales_strategy_sha256(
        changed_content
    )


def test_canonical_content_sha_is_stable_across_storage_version_and_time() -> None:
    value = SalesStrategyInputV1.model_validate(strategy_payload())
    next_record = value.model_copy(
        update={
            "strategy_input_version": 2,
            "created_at": datetime(2026, 8, 31, 9, tzinfo=UTC),
        }
    )
    assert canonical_sales_strategy_sha256(value) == canonical_sales_strategy_sha256(next_record)


def test_canonical_content_sha_is_stable_across_delivery_event_identity() -> None:
    value = SalesStrategyInputV1.model_validate(strategy_payload())
    same_content_redelivered = value.model_copy(
        update={"source_album_event_id": UUID("99999999-9999-4999-8999-999999999999")}
    )
    assert canonical_sales_strategy_sha256(value) == canonical_sales_strategy_sha256(
        same_content_redelivered
    )


def test_update_event_is_strict_and_carries_no_full_strategy() -> None:
    event = SalesStrategyInputUpdatedV1.model_validate(
        {
            "schema_version": 1,
            "event_id": "33333333-3333-4333-8333-333333333333",
            "occurred_at": "2026-08-31T08:01:00Z",
            "tenant_id": "22222222-2222-4222-8222-222222222222",
            "product_code": "SYNTHETIC-001",
            "strategy_input_version": 1,
            "product_data_version": 1,
            "status": "preview_only",
            "content_sha256": "c" * 64,
            "automatic_customer_contact_allowed": False,
            "automatic_publication_allowed": False,
            "automatic_wechat_send_allowed": False,
            "automatic_quote_allowed": False,
        }
    )
    assert event.status == "preview_only"
    invalid = deepcopy(event.model_dump(mode="json"))
    invalid["images"] = []
    with pytest.raises(ValidationError):
        SalesStrategyInputUpdatedV1.model_validate(invalid)
