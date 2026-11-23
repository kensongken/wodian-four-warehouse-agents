from datetime import UTC, datetime
from importlib import import_module
from uuid import UUID

import pytest
from pydantic import ValidationError


def contract_model(module_name: str, model_name: str):
    module = import_module(f"product_engine.contracts.{module_name}")
    return getattr(module, model_name)


def event_envelope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "11111111-1111-4111-8111-111111111111",
        "occurred_at": "2026-08-15T08:00:00Z",
        "tenant_id": "22222222-2222-4222-8222-222222222222",
    }


def material_asset() -> dict[str, object]:
    return {
        "material_id": "55555555-5555-4555-8555-555555555555",
        "upstream_asset_id": "44444444-4444-4444-8444-444444444444",
        "object_key": "tenants/222/products/SP-00831/source/main.heic",
        "sha256": "a" * 64,
        "mime_type": "image/heic",
        "role": "main_candidate",
    }


def test_product_image_set_contract_round_trips(valid_album_payload) -> None:
    model = contract_model("album", "ProductImageSetReadyV1")

    event = model.model_validate(valid_album_payload)

    assert event.event_id == UUID(str(valid_album_payload["event_id"]))
    assert event.occurred_at == datetime(2026, 8, 15, 8, tzinfo=UTC)
    assert event.images[0].mime_type == "image/heic"


def test_product_image_set_rejects_invalid_inputs(valid_album_payload) -> None:
    model = contract_model("album", "ProductImageSetReadyV1")
    image = valid_album_payload["images"][0]
    cases = [
        {**valid_album_payload, "schema_version": 2},
        {**valid_album_payload, "occurred_at": "2026-08-15T16:00:00+08:00"},
        {**valid_album_payload, "images": []},
        {**valid_album_payload, "images": [{**image, "sha256": "bad"}]},
        {**valid_album_payload, "images": [{**image, "mime_type": "image/gif"}]},
        {**valid_album_payload, "classification_confidence": 1.1},
        {**valid_album_payload, "product_code": "X" * 65},
        {**valid_album_payload, "unexpected": "not-allowed"},
    ]

    for payload in cases:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_product_materials_accepts_only_source_assets() -> None:
    model = contract_model("materials", "ProductMaterialsUpdatedV1")
    payload = {
        **event_envelope(),
        "product_code": "SP-00831",
        "package_version": 1,
        "status": "source_ready",
        "source_assets": [material_asset()],
        "generated_assets": [],
        "content_status": "pending",
        "price_status": "pending",
    }

    event = model.model_validate(payload)

    assert event.package_version == 1
    assert event.generated_assets == []
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "generated_assets": [material_asset()]})


def test_sales_usage_accepts_only_traceable_material_actions() -> None:
    model = contract_model("sales", "SalesMaterialUsageV1")
    payload = {
        **event_envelope(),
        "session_id": "66666666-6666-4666-8666-666666666666",
        "product_code": "SP-00831",
        "package_version": 1,
        "usage_type": "copied",
        "material_ids": ["55555555-5555-4555-8555-555555555555"],
    }

    event = model.model_validate(payload)

    assert event.usage_type == "copied"
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "usage_type": "sale_completed"})


def test_screenshot_import_enforces_type_and_twenty_mib_limit() -> None:
    model = contract_model("screenshots", "ScreenshotImportedV1")
    payload = {
        **event_envelope(),
        "screenshot_import_id": "77777777-7777-4777-8777-777777777777",
        "object_key": "tenants/222/screenshots/order-001.png",
        "sha256": "b" * 64,
        "mime_type": "image/png",
        "byte_size": 20_971_520,
        "session_id": "66666666-6666-4666-8666-666666666666",
        "product_code": "SP-00831",
    }

    event = model.model_validate(payload)

    assert event.byte_size == 20_971_520
    for invalid in (
        {**payload, "byte_size": 20_971_521},
        {**payload, "mime_type": "image/gif"},
    ):
        with pytest.raises(ValidationError):
            model.model_validate(invalid)
