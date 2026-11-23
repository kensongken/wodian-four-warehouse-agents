from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.contracts.materials import ProductMaterialsUpdatedV1
from product_engine.services.sales_strategy_inputs import (
    build_preview_sales_strategy_input,
    canonical_album_sha256,
)


def test_album_materials_build_traceable_preview_strategy(valid_album_payload) -> None:
    album = ProductImageSetReadyV1.model_validate(valid_album_payload)
    materials = ProductMaterialsUpdatedV1(
        schema_version=1,
        event_id="33333333-3333-4333-8333-333333333333",
        occurred_at="2026-08-31T08:01:00Z",
        tenant_id=album.tenant_id,
        product_code=album.product_code,
        package_version=2,
        status="source_ready",
        source_assets=[
            {
                "material_id": "55555555-5555-4555-8555-555555555555",
                "upstream_asset_id": album.images[0].asset_id,
                "object_key": album.images[0].object_key,
                "sha256": album.images[0].sha256,
                "mime_type": album.images[0].mime_type,
                "role": album.images[0].role,
            }
        ],
        content_status="pending",
        price_status="pending",
    )

    strategy = build_preview_sales_strategy_input(album, materials)

    assert (strategy.tenant_id, strategy.product_code) == (
        album.tenant_id,
        album.product_code,
    )
    assert strategy.source_album_event_id == album.event_id
    assert strategy.source_material_package_version == 2
    assert strategy.source_image_set_sha256 == canonical_album_sha256(album)
    assert strategy.images[0].asset_id == album.images[0].asset_id
    assert strategy.material.value == album.category
    assert strategy.form.value == album.form
    assert strategy.status == "preview_only"
    assert strategy.inventory_status == "pending"
    assert strategy.price_status == "pending"
    assert strategy.automatic_customer_contact_allowed is False
    assert "object_key" not in strategy.model_dump(mode="json")["images"][0]
