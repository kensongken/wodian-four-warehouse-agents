import hashlib
import json
from datetime import UTC, datetime

from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.contracts.materials import ProductMaterialsUpdatedV1
from product_engine.contracts.sales_strategy import (
    CandidateProductFact,
    SalesStrategyImageRef,
    SalesStrategyInputV1,
    canonical_sales_strategy_sha256,
)


def canonical_album_sha256(event: ProductImageSetReadyV1) -> str:
    body = event.model_dump(
        mode="json",
        exclude={"event_id", "occurred_at"},
    )
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_preview_sales_strategy_input(
    album_event: ProductImageSetReadyV1,
    material_package: ProductMaterialsUpdatedV1,
) -> SalesStrategyInputV1:
    if (
        album_event.tenant_id != material_package.tenant_id
        or album_event.product_code != material_package.product_code
    ):
        raise ValueError("sales_strategy_source_identity_mismatch")
    provisional = SalesStrategyInputV1(
        tenant_id=album_event.tenant_id,
        product_code=album_event.product_code,
        strategy_input_version=1,
        product_data_version=material_package.package_version,
        source_album_event_id=album_event.event_id,
        source_material_package_version=material_package.package_version,
        source_image_set_sha256=canonical_album_sha256(album_event),
        images=[
            SalesStrategyImageRef(
                asset_id=asset.upstream_asset_id,
                material_id=asset.material_id,
                sha256=asset.sha256,
                mime_type=asset.mime_type,
                role=asset.role,
            )
            for asset in material_package.source_assets
        ],
        material=CandidateProductFact(value=album_event.category),
        form=CandidateProductFact(value=album_event.form),
        visual_observations=[],
        selling_point_candidates=[],
        unverified_items=["材质待人工确认", "真伪未验证", "产地未验证"],
        forbidden_expressions=["官方保真", "保证升值", "固定成交价"],
        created_at=datetime.now(UTC),
        content_sha256="0" * 64,
    )
    return provisional.model_copy(
        update={"content_sha256": canonical_sales_strategy_sha256(provisional)}
    )
