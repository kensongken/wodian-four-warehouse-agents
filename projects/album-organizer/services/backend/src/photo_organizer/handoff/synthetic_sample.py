from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from photo_organizer.handoff.product_image_set import (
    ClassificationCandidateSource,
    ConfirmedClassificationSource,
    OpcExternalTenantBinding,
    PreparedImageHandoff,
    ProductCodeBinding,
    ProductImageSetSource,
    SourceImage,
    build_product_image_set_ready,
)


def generate_synthetic_sample(output: Path) -> Path:
    tenant_id = UUID("22222222-2222-4222-8222-222222222222")
    asset_id = UUID("44444444-4444-4444-8444-444444444444")
    source = ProductImageSetSource(
        opc_id="synthetic-opc-001",
        album_product_id="album-product-synthetic-001",
        snapshot_version=1,
        source_import_task_id="33333333-3333-4333-8333-333333333333",
        images=(
            SourceImage(
                asset_id=asset_id,
                sha256="a" * 64,
                mime_type="image/png",
                kind="original",
            ),
        ),
    )
    classification = ConfirmedClassificationSource(
        classification_id=UUID("55555555-5555-4555-8555-555555555555"),
        opc_id=source.opc_id,
        album_product_id=source.album_product_id,
        input_snapshot_version=source.snapshot_version,
        status="confirmed",
        confirmed_level_1="南红",
        confirmed_level_2="手串",
        confirmed_at=datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
        candidates=(
            ClassificationCandidateSource(
                level_1="南红",
                level_2="手串",
                confidence=0.8,
            ),
        ),
    )
    tenant_binding = OpcExternalTenantBinding(
        binding_id=UUID("66666666-6666-4666-8666-666666666666"),
        binding_source="operator_provisioned",
        opc_id=source.opc_id,
        tenant_id=tenant_id,
    )
    product_binding = ProductCodeBinding(
        binding_id=UUID("77777777-7777-4777-8777-777777777777"),
        binding_source="operator_provisioned",
        opc_id=source.opc_id,
        album_product_id=source.album_product_id,
        product_code="SYNTHETIC-001",
    )
    event = build_product_image_set_ready(
        source,
        classification,
        tenant_binding,
        product_binding,
        (
            PreparedImageHandoff(
                asset_id=asset_id,
                object_key=(f"tenants/{tenant_id}/products/SYNTHETIC-001/main.png"),
                role="main_candidate",
                quality_confidence=0.9,
                storage_status="ready",
            ),
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output.resolve()
