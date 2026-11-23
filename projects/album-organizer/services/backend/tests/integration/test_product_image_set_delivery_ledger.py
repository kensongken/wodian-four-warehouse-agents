from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.handoff.delivery_ledger import ProductImageSetDeliveryLedger
from photo_organizer.handoff.delivery_queue import ProductImageSetDeliveryQueue
from photo_organizer.handoff.product_image_set import HandoffAdapterError
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
OPC_ID = "synthetic-handoff-opc"
ALT_OPC_ID = "synthetic-handoff-alt-opc"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
SNAPSHOT_EVENT_ID = UUID("55555555-5555-4555-8555-555555555555")
CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")


async def _clean(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(delete(ProductImageSetDeliveryImage))
        await session.execute(delete(ProductImageSetDelivery))
        await session.execute(delete(PreparedImageHandoffRecord))
        await session.execute(delete(ProductCodeBindingRecord))
        await session.execute(delete(OpcExternalTenantBindingRecord))
        await session.execute(delete(ProductClassification))
        await session.execute(delete(AlbumProductSnapshot))
        await session.execute(
            delete(OpcWorkspace).where(OpcWorkspace.id.in_((OPC_ID, ALT_OPC_ID)))
        )
        await session.commit()


@pytest.mark.asyncio
async def test_two_postgres_sessions_create_once_then_replay(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _clean(auth_session_factory)
    try:
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id=OPC_ID, display_name="Synthetic Handoff OPC"))
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
                candidate_event_id=UUID("77777777-7777-4777-8777-777777777777"),
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
                object_key=f"tenants/{TENANT_ID}/synthetic/main.png",
                role="main_candidate",
                quality_confidence=0.9,
                storage_status="ready",
                prepared_by="synthetic-operator",
            )
            session.add(prepared)
            await session.commit()
            product_id = product.id
            prepared_id = prepared.id

        first, second = await asyncio.gather(
            *(
                ProductImageSetDeliveryQueue(
                    auth_session_factory
                ).enqueue_pending(limit=10)
                for _ in range(2)
            )
        )

        assert first.created + second.created == 1
        assert first.replayed + second.replayed in {0, 1}
        assert first.blocked == second.blocked == {}
        async with auth_session_factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(ProductImageSetDelivery)
            )
            stored = await session.scalar(select(ProductImageSetDelivery))
            linked_count = await session.scalar(
                select(func.count()).select_from(ProductImageSetDeliveryImage)
            )
        assert count == 1
        assert linked_count == 1
        assert stored is not None
        assert stored.payload["product_code"] == "SYNTHETIC-001"

        async with auth_session_factory() as session:
            session.add(
                OpcWorkspace(id=ALT_OPC_ID, display_name="Synthetic Alternate OPC")
            )
            await session.flush()
            alternate_tenant = OpcExternalTenantBindingRecord(
                opc_id=ALT_OPC_ID,
                tenant_id=UUID("88888888-8888-4888-8888-888888888888"),
                binding_source="operator_provisioned",
                provisioned_by="synthetic-operator",
            )
            session.add(alternate_tenant)
            await session.commit()
            alternate_tenant_id = alternate_tenant.id

        with pytest.raises(HandoffAdapterError, match="event_identity_conflict"):
            await ProductImageSetDeliveryLedger(
                auth_session_factory
            ).record_from_authority(
                source_classification_id=CLASSIFICATION_ID,
                tenant_binding_id=alternate_tenant_id,
                product_binding_id=product_id,
                prepared_image_binding_ids=(prepared_id,),
            )

        async with auth_session_factory() as session:
            unchanged = await session.scalar(select(ProductImageSetDelivery))
            linked_count_after_conflict = await session.scalar(
                select(func.count()).select_from(ProductImageSetDeliveryImage)
            )
            assert unchanged is not None
            assert unchanged.payload == stored.payload
            assert unchanged.canonical_sha256 == stored.canonical_sha256
            assert linked_count_after_conflict == 1

            snapshot = await session.scalar(select(AlbumProductSnapshot))
            assert snapshot is not None
            await session.delete(snapshot)
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
    finally:
        await _clean(auth_session_factory)
