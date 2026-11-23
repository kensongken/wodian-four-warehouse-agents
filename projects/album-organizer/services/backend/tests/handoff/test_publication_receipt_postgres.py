from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.handoff.delivery_ledger import (
    ProductImageSetDeliveryLedger,
    PublicationReceiptState,
)
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.handoff_authority import (
    OpcExternalTenantBindingRecord,
    PreparedImageHandoffRecord,
    ProductCodeBindingRecord,
)
from photo_organizer.models.opc import OpcWorkspace
from photo_organizer.models.product_image_set_delivery import (
    ProductImageSetDelivery,
    ProductImageSetDeliveryImage,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
OPC_ID = "synthetic-opc-001"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
SNAPSHOT_EVENT_ID = UUID("95555555-5555-4555-8555-555555555555")
CLASSIFICATION_ID = UUID("55555555-5555-4555-8555-555555555555")


async def _clean(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(delete(ProductImageSetDeliveryImage))
        await session.execute(delete(ProductImageSetDelivery))
        await session.execute(delete(PreparedImageHandoffRecord))
        await session.execute(delete(ProductCodeBindingRecord))
        await session.execute(delete(OpcExternalTenantBindingRecord))
        await session.execute(delete(ProductClassification))
        await session.execute(delete(AlbumProductSnapshot))
        await session.execute(delete(OpcWorkspace).where(OpcWorkspace.id == OPC_ID))
        await session.commit()


async def _record_delivery(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, str]:
    async with factory() as session:
        session.add(OpcWorkspace(id=OPC_ID, display_name="Synthetic Publication OPC"))
        await session.flush()
        snapshot = AlbumProductSnapshot(
            source_event_id=SNAPSHOT_EVENT_ID,
            opc_id=OPC_ID,
            album_product_id="album-product-synthetic-001",
            snapshot_version=1,
            source_import_task_id=str(TASK_ID),
            source_type="first_sale_bead",
            occurred_at=NOW,
            group_confirmed_by="synthetic-operator",
            group_confirmed_at=NOW,
            consent_basis="opc_owned_product_material",
            evidence_resolution_mode="registry_required",
            allowed_consumers=["categorization"],
            images=[
                {
                    "asset_id": str(ASSET_ID),
                    "sha256": "a" * 64,
                    "media_type": "image/png",
                    "role": "original",
                }
            ],
            source_payload={"synthetic_only": True},
        )
        classification = ProductClassification(
            id=CLASSIFICATION_ID,
            opc_id=OPC_ID,
            album_product_id=snapshot.album_product_id,
            source_type="first_sale_bead",
            input_event_id=SNAPSHOT_EVENT_ID,
            candidate_event_id=UUID("97777777-7777-4777-8777-777777777777"),
            input_snapshot_version=1,
            classification_version=1,
            engine_name="synthetic-engine",
            engine_version="synthetic-v1",
            candidates=[{"level_1": "南红", "level_2": "手串", "confidence": 0.8}],
            status="confirmed",
            confirmed_level_1="南红",
            confirmed_level_2="手串",
            confirmed_by="synthetic-operator",
            confirmed_at=NOW,
        )
        tenant = OpcExternalTenantBindingRecord(
            opc_id=OPC_ID,
            tenant_id=TENANT_ID,
            binding_source="operator_provisioned",
            provisioned_by="synthetic-operator",
        )
        product = ProductCodeBindingRecord(
            opc_id=OPC_ID,
            album_product_id=snapshot.album_product_id,
            product_code="SYNTHETIC-001",
            binding_source="operator_provisioned",
            provisioned_by="synthetic-operator",
        )
        session.add_all([snapshot, classification, tenant, product])
        await session.flush()
        prepared = PreparedImageHandoffRecord(
            product_binding_id=product.id,
            source_classification_id=classification.id,
            asset_id=ASSET_ID,
            object_key=f"tenants/{TENANT_ID}/products/SYNTHETIC-001/main.png",
            role="main_candidate",
            quality_confidence=0.9,
            storage_status="ready",
            prepared_by="synthetic-operator",
        )
        session.add(prepared)
        await session.commit()
        tenant_binding_id = tenant.id
        product_binding_id = product.id
        prepared_id = prepared.id

    receipt = await ProductImageSetDeliveryLedger(factory).record_from_authority(
        source_classification_id=CLASSIFICATION_ID,
        tenant_binding_id=tenant_binding_id,
        product_binding_id=product_binding_id,
        prepared_image_binding_ids=(prepared_id,),
    )
    async with factory() as session:
        stored = await session.scalar(
            select(ProductImageSetDelivery).where(
                ProductImageSetDelivery.event_id == receipt.event_id
            )
        )
        assert stored is not None
        return receipt.event_id, stored.canonical_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "status_code",
        "expected_status",
        "expected_error",
        "expects_retry",
        "expects_delivered_at",
    ),
    (
        (202, "delivered", None, False, True),
        (503, "retryable_failure", "http_503", True, False),
        (409, "permanent_failure", "http_409", False, False),
    ),
)
async def test_publication_receipt_persists_after_new_session(
    auth_session_factory: async_sessionmaker[AsyncSession],
    status_code: int,
    expected_status: str,
    expected_error: str | None,
    expects_retry: bool,
    expects_delivered_at: bool,
) -> None:
    await _clean(auth_session_factory)
    try:
        event_id, digest = await _record_delivery(auth_session_factory)

        await ProductImageSetDeliveryLedger(
            auth_session_factory
        ).record_publication_receipt(
            event_id=event_id,
            canonical_sha256=digest,
            state=PublicationReceiptState.from_http_result(status_code, attempt=1),
        )

        async with auth_session_factory() as session:
            stored = await session.scalar(
                select(ProductImageSetDelivery).where(
                    ProductImageSetDelivery.event_id == event_id
                )
            )
            assert stored is not None
            assert stored.publication_status == expected_status
            assert stored.publication_attempts == 1
            assert (stored.publication_next_attempt_at is not None) is expects_retry
            assert stored.publication_last_error_code == expected_error
            assert (stored.publication_delivered_at is not None) is expects_delivered_at
    finally:
        await _clean(auth_session_factory)
