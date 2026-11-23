import json
from datetime import UTC, datetime
from pathlib import Path

from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.contracts.materials import ProductMaterialsUpdatedV1
from product_engine.contracts.sales_strategy import canonical_sales_strategy_sha256
from product_engine.services.sales_strategy_inputs import build_preview_sales_strategy_input

ROOT = Path(__file__).resolve().parents[4]


def test_album_candidate_generates_the_frozen_three_stage_sales_snapshot() -> None:
    album_payload = json.loads(
        (ROOT / "fixtures/v0.1/product-image-set-ready.album-95daa.synthetic.json").read_text()
    )
    album = ProductImageSetReadyV1.model_validate(album_payload)
    asset = album.images[0]
    materials = ProductMaterialsUpdatedV1(
        schema_version=1,
        event_id="33333333-3333-4333-8333-333333333334",
        occurred_at="2026-08-31T08:01:00Z",
        tenant_id=album.tenant_id,
        product_code=album.product_code,
        package_version=1,
        status="source_ready",
        source_assets=[
            {
                "material_id": "55555555-5555-4555-8555-555555555555",
                "upstream_asset_id": asset.asset_id,
                "object_key": asset.object_key,
                "sha256": asset.sha256,
                "mime_type": asset.mime_type,
                "role": asset.role,
            }
        ],
        content_status="pending",
        price_status="pending",
    )

    generated = build_preview_sales_strategy_input(album, materials).model_copy(
        update={
            "created_at": datetime(2026, 8, 31, 8, 2, tzinfo=UTC),
            "content_sha256": "0" * 64,
        }
    )
    generated = generated.model_copy(
        update={
            "content_sha256": canonical_sales_strategy_sha256(generated),
        }
    )
    expected = json.loads(
        (ROOT / "fixtures/v0.1/sales-strategy-input.three-stage.synthetic.json").read_text()
    )

    assert generated.model_dump(mode="json") == expected
    assert str(generated.source_album_event_id) == album_payload["event_id"]
    assert str(generated.images[0].material_id) == "55555555-5555-4555-8555-555555555555"
    assert generated.images[0].sha256 == album_payload["images"][0]["sha256"]


def test_album_version_two_fixture_preserves_product_identity() -> None:
    first = ProductImageSetReadyV1.model_validate_json(
        (ROOT / "fixtures/v0.1/product-image-set-ready.album-95daa.synthetic.json").read_bytes()
    )
    second = ProductImageSetReadyV1.model_validate_json(
        (ROOT / "fixtures/v0.1/product-image-set-ready.album-v2.synthetic.json").read_bytes()
    )

    assert second.tenant_id == first.tenant_id
    assert second.product_code == first.product_code == "SYNTHETIC-001"
    assert second.event_id != first.event_id
    assert second.source_task_id != first.source_task_id
    assert second.images[0].asset_id != first.images[0].asset_id
    assert second.images[0].sha256 != first.images[0].sha256
