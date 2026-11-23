from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.group_confirmation import (
    AlbumProductGroupConfirmation,
    AlbumProductGroupConfirmationService,
    EvidenceGroupConflict,
    EvidenceGroupNotFound,
    ExistingProductConfirmationConflict,
    ExistingProductNotFound,
    OpcOwnerRequired,
    SnapshotVersionConflict,
)
from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    CategorizationRequest,
    ConfirmedSnapshot,
    ConfirmedSnapshotBatch,
    EngineCandidate,
    Level1Category,
    Level2Category,
    ProductSourceType,
    ReasonCode,
)
from photo_organizer.categorization.stream import (
    RESULT_TOPIC,
    CategorizationStreamHandler,
)
from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.categorization_delivery import CategorizationDelivery
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.handoff_authority import ProductCodeBindingRecord
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User
from photo_organizer.services.outbox_publisher import OutboxPublisher

OPC_ID = "opc-group-confirmation"
NOW = datetime(2026, 8, 17, 20, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def clear_group_confirmation_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
):
    async def clear() -> None:
        async with auth_session_factory() as session:
            await session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.topic.in_(
                        (
                            "album.product.snapshot.confirmed.v1",
                            "album.product.classification.candidates.v1",
                            "unrelated.test",
                        )
                    )
                )
            )
            await session.execute(delete(CategorizationDelivery))
            await session.execute(
                delete(ProductCodeBindingRecord).where(
                    ProductCodeBindingRecord.opc_id.in_((OPC_ID, "opc-group-other"))
                )
            )
            await session.execute(delete(AlbumProductSnapshot))
            await session.execute(delete(OpcEvidenceAsset))
            await session.execute(
                delete(Asset).where(Asset.local_asset_id.like("group-confirm-%"))
            )
            await session.execute(
                delete(ImportTask).where(
                    ImportTask.name == "Group confirmation fixture"
                )
            )
            await session.execute(
                delete(StoredBlob).where(
                    StoredBlob.object_key.like("private/group-confirm/%")
                )
            )
            await session.execute(
                delete(OpcWorkspace).where(
                    OpcWorkspace.id.in_((OPC_ID, "opc-group-other"))
                )
            )
            await session.commit()

    await clear()
    yield
    await clear()


@dataclass(frozen=True)
class SeededEvidenceGroup:
    user_id: UUID
    task_id: UUID
    evidence_ids: tuple[UUID, ...]


class SeedEchoEngine:
    engine_name = "group-confirmation-test"
    engine_version = "1.0"

    def __init__(self) -> None:
        self.requests: list[CategorizationRequest] = []

    async def classify(
        self,
        request: CategorizationRequest,
    ) -> tuple[EngineCandidate, ...]:
        self.requests.append(request)
        return (
            EngineCandidate(
                level_1=request.seed.level_1_candidate,
                level_2=request.seed.level_2_candidate,
                confidence=0.9,
                reason_codes=(ReasonCode.SEED_MATCH,),
            ),
        )


async def _seed_evidence_group(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 2,
) -> SeededEvidenceGroup:
    user = User(id=uuid4(), phone_e164=f"+86134{str(uuid4().int)[-8:]}")
    task = ImportTask(
        id=uuid4(),
        user_id=user.id,
        name="Group confirmation fixture",
        status=TaskStatus.UPLOADING,
    )
    blobs: list[StoredBlob] = []
    assets: list[Asset] = []
    evidences: list[OpcEvidenceAsset] = []
    for index in range(count):
        sha256 = f"{index + 1:064x}"
        blob = StoredBlob(
            id=uuid4(),
            user_id=user.id,
            sha256=sha256,
            byte_size=1000 + index,
            object_key=f"private/group-confirm/{user.id}/{uuid4()}/original",
            is_ready=True,
        )
        asset = Asset(
            id=uuid4(),
            user_id=user.id,
            task_id=task.id,
            blob_id=blob.id,
            local_asset_id=f"group-confirm-{uuid4()}",
            filename=f"group-{index}.jpg",
            media_type="image/jpeg",
            byte_size=blob.byte_size,
            sha256=blob.sha256,
            captured_at=NOW,
            status=AssetStatus.UPLOADED,
        )
        evidence = OpcEvidenceAsset(
            id=uuid4(),
            opc_id=OPC_ID,
            source_asset_id=asset.id,
            source_blob_id=blob.id,
            created_by=user.id,
            storage_ref="",
            source_object_key=blob.object_key,
            sha256=blob.sha256,
            byte_size=blob.byte_size,
            media_type="image/jpeg",
            captured_at=asset.captured_at,
            imported_at=NOW,
        )
        evidence.storage_ref = f"evidence/{evidence.id}/original"
        blobs.append(blob)
        assets.append(asset)
        evidences.append(evidence)
    async with session_factory() as session:
        session.add_all(
            [user, OpcWorkspace(id=OPC_ID, display_name="Group Confirmation OPC")]
        )
        await session.flush()
        session.add(OpcMembership(opc_id=OPC_ID, user_id=user.id, role="reviewer"))
        session.add_all([task, *blobs])
        await session.flush()
        session.add_all(assets)
        await session.flush()
        for asset, evidence in zip(assets, evidences, strict=True):
            evidence.imported_at = asset.created_at
        session.add_all(evidences)
        await session.commit()
    return SeededEvidenceGroup(
        user_id=user.id,
        task_id=task.id,
        evidence_ids=tuple(evidence.id for evidence in evidences),
    )


async def _seed_additional_evidence_group(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    marker: int,
) -> SeededEvidenceGroup:
    task = ImportTask(
        id=uuid4(),
        user_id=user_id,
        name="Group confirmation fixture",
        status=TaskStatus.UPLOADING,
    )
    evidence_ids: list[UUID] = []
    async with session_factory() as session:
        session.add(task)
        await session.flush()
        for index in range(2):
            blob = StoredBlob(
                id=uuid4(),
                user_id=user_id,
                sha256=f"{marker * 100 + index:064x}",
                byte_size=2000 + marker * 10 + index,
                object_key=f"private/group-confirm/{user_id}/{uuid4()}/original",
                is_ready=True,
            )
            session.add(blob)
            await session.flush()
            asset = Asset(
                id=uuid4(),
                user_id=user_id,
                task_id=task.id,
                blob_id=blob.id,
                local_asset_id=f"group-confirm-{uuid4()}",
                filename=f"group-additional-{marker}-{index}.jpg",
                media_type="image/jpeg",
                byte_size=blob.byte_size,
                sha256=blob.sha256,
                captured_at=NOW,
                status=AssetStatus.UPLOADED,
            )
            session.add(asset)
            await session.flush()
            evidence = OpcEvidenceAsset(
                id=uuid4(),
                opc_id=OPC_ID,
                source_asset_id=asset.id,
                source_blob_id=blob.id,
                created_by=user_id,
                storage_ref="",
                source_object_key=blob.object_key,
                sha256=blob.sha256,
                byte_size=blob.byte_size,
                media_type=asset.media_type,
                captured_at=asset.captured_at,
                imported_at=asset.created_at,
            )
            evidence.storage_ref = f"evidence/{evidence.id}/original"
            session.add(evidence)
            evidence_ids.append(evidence.id)
        await session.commit()
    return SeededEvidenceGroup(
        user_id=user_id,
        task_id=task.id,
        evidence_ids=tuple(evidence_ids),
    )


async def _prepare_bound_product(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[AlbumProductGroupConfirmationService, SeededEvidenceGroup, str]:
    seeded = await _seed_evidence_group(session_factory)
    service = AlbumProductGroupConfirmationService(session_factory, clock=lambda: NOW)
    initial = await service.confirm(
        opc_id=OPC_ID,
        user_id=seeded.user_id,
        confirmation_request_id=uuid4(),
        evidence_asset_ids=seeded.evidence_ids,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        level_1_candidate=Level1Category.NAN_HONG,
        level_2_candidate=Level2Category.BRACELET,
    )
    async with session_factory() as session:
        membership = await session.scalar(
            select(OpcMembership).where(
                OpcMembership.opc_id == OPC_ID,
                OpcMembership.user_id == seeded.user_id,
            )
        )
        assert membership is not None
        membership.role = "owner"
        session.add(
            ProductCodeBindingRecord(
                opc_id=OPC_ID,
                album_product_id=initial.album_product_id,
                product_code="SYNTHETIC-001",
                binding_source="operator_provisioned",
                provisioned_by="synthetic-operator",
            )
        )
        await session.commit()
    return service, seeded, initial.album_product_id


def _next_snapshot_kwargs(
    *,
    seeded: SeededEvidenceGroup,
    album_product_id: str,
    request_id: UUID | None = None,
) -> dict[str, object]:
    return {
        "opc_id": OPC_ID,
        "user_id": seeded.user_id,
        "album_product_id": album_product_id,
        "confirmation_request_id": request_id or uuid4(),
        "expected_current_snapshot_version": 1,
        "evidence_asset_ids": seeded.evidence_ids,
        "source_type": ProductSourceType.FIRST_SALE_BEAD,
        "level_1_candidate": Level1Category.NAN_HONG,
        "level_2_candidate": Level2Category.BRACELET,
    }


@pytest.mark.asyncio
async def test_next_snapshot_requires_opc_owner(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, first, product_id = await _prepare_bound_product(auth_session_factory)
    second = await _seed_additional_evidence_group(
        auth_session_factory, user_id=first.user_id, marker=11
    )
    async with auth_session_factory() as session:
        membership = await session.scalar(
            select(OpcMembership).where(
                OpcMembership.opc_id == OPC_ID,
                OpcMembership.user_id == first.user_id,
            )
        )
        assert membership is not None
        membership.role = "reviewer"
        await session.commit()

    with pytest.raises(OpcOwnerRequired, match="^opc_owner_required$"):
        await service.confirm_next_snapshot(
            **_next_snapshot_kwargs(seeded=second, album_product_id=product_id)
        )


@pytest.mark.asyncio
async def test_next_snapshot_fails_closed_for_unknown_or_unbound_product(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, first, product_id = await _prepare_bound_product(auth_session_factory)
    second = await _seed_additional_evidence_group(
        auth_session_factory, user_id=first.user_id, marker=12
    )
    async with auth_session_factory() as session:
        binding = await session.scalar(
            select(ProductCodeBindingRecord).where(
                ProductCodeBindingRecord.opc_id == OPC_ID,
                ProductCodeBindingRecord.album_product_id == product_id,
            )
        )
        assert binding is not None
        await session.delete(binding)
        await session.commit()

    with pytest.raises(ExistingProductNotFound, match="^existing_product_not_found$"):
        await service.confirm_next_snapshot(
            **_next_snapshot_kwargs(seeded=second, album_product_id=product_id)
        )


@pytest.mark.asyncio
async def test_next_snapshot_rejects_stale_version_and_changed_replay(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, first, product_id = await _prepare_bound_product(auth_session_factory)
    second = await _seed_additional_evidence_group(
        auth_session_factory, user_id=first.user_id, marker=13
    )
    kwargs = _next_snapshot_kwargs(seeded=second, album_product_id=product_id)
    await service.confirm_next_snapshot(**kwargs)

    with pytest.raises(SnapshotVersionConflict, match="^snapshot_version_conflict$"):
        await service.confirm_next_snapshot(
            **_next_snapshot_kwargs(seeded=second, album_product_id=product_id)
        )
    kwargs["expected_current_snapshot_version"] = 2
    with pytest.raises(
        ExistingProductConfirmationConflict,
        match="^existing_product_confirmation_conflict$",
    ):
        await service.confirm_next_snapshot(**kwargs)


@pytest.mark.asyncio
async def test_next_snapshot_replays_exact_request_without_creating_version_three(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, first, product_id = await _prepare_bound_product(auth_session_factory)
    second = await _seed_additional_evidence_group(
        auth_session_factory, user_id=first.user_id, marker=14
    )
    kwargs = _next_snapshot_kwargs(seeded=second, album_product_id=product_id)

    created = await service.confirm_next_snapshot(**kwargs)
    replayed = await service.confirm_next_snapshot(**kwargs)

    assert replayed == created
    async with auth_session_factory() as session:
        versions = list(
            await session.scalars(
                select(AlbumProductSnapshot.snapshot_version)
                .where(AlbumProductSnapshot.album_product_id == product_id)
                .order_by(AlbumProductSnapshot.snapshot_version)
            )
        )
    assert versions == [1, 2]


@pytest.mark.asyncio
async def test_concurrent_next_snapshots_have_one_winner(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service, first, product_id = await _prepare_bound_product(auth_session_factory)
    second = await _seed_additional_evidence_group(
        auth_session_factory, user_id=first.user_id, marker=15
    )
    third = await _seed_additional_evidence_group(
        auth_session_factory, user_id=first.user_id, marker=16
    )

    results = await asyncio.gather(
        service.confirm_next_snapshot(
            **_next_snapshot_kwargs(seeded=second, album_product_id=product_id)
        ),
        service.confirm_next_snapshot(
            **_next_snapshot_kwargs(seeded=third, album_product_id=product_id)
        ),
        return_exceptions=True,
    )

    assert (
        sum(isinstance(result, AlbumProductGroupConfirmation) for result in results)
        == 1
    )
    assert sum(isinstance(result, SnapshotVersionConflict) for result in results) == 1
    async with auth_session_factory() as session:
        versions = list(
            await session.scalars(
                select(AlbumProductSnapshot.snapshot_version)
                .where(AlbumProductSnapshot.album_product_id == product_id)
                .order_by(AlbumProductSnapshot.snapshot_version)
            )
        )
    assert versions == [1, 2]


@pytest.mark.asyncio
async def test_owner_appends_new_batch_to_existing_bound_product(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_batch = await _seed_evidence_group(auth_session_factory)
    service = AlbumProductGroupConfirmationService(
        auth_session_factory,
        clock=lambda: NOW,
    )
    initial = await service.confirm(
        opc_id=OPC_ID,
        user_id=first_batch.user_id,
        confirmation_request_id=uuid4(),
        evidence_asset_ids=first_batch.evidence_ids,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        level_1_candidate=Level1Category.NAN_HONG,
        level_2_candidate=Level2Category.BRACELET,
    )
    async with auth_session_factory() as session:
        membership = await session.scalar(
            select(OpcMembership).where(
                OpcMembership.opc_id == OPC_ID,
                OpcMembership.user_id == first_batch.user_id,
            )
        )
        assert membership is not None
        membership.role = "owner"
        binding = ProductCodeBindingRecord(
            opc_id=OPC_ID,
            album_product_id=initial.album_product_id,
            product_code="SYNTHETIC-001",
            binding_source="operator_provisioned",
            provisioned_by="synthetic-operator",
        )
        session.add(binding)
        await session.commit()
        binding_id = binding.id
    second_batch = await _seed_additional_evidence_group(
        auth_session_factory,
        user_id=first_batch.user_id,
        marker=2,
    )
    request_id = uuid4()

    result = await service.confirm_next_snapshot(
        opc_id=OPC_ID,
        user_id=first_batch.user_id,
        album_product_id=initial.album_product_id,
        confirmation_request_id=request_id,
        expected_current_snapshot_version=1,
        evidence_asset_ids=second_batch.evidence_ids,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        level_1_candidate=Level1Category.NAN_HONG,
        level_2_candidate=Level2Category.BRACELET,
    )

    assert result.album_product_id == initial.album_product_id
    assert result.snapshot_version == 2
    assert result.confirmation_event_id == request_id
    async with auth_session_factory() as session:
        snapshots = list(
            (
                await session.scalars(
                    select(AlbumProductSnapshot)
                    .where(
                        AlbumProductSnapshot.opc_id == OPC_ID,
                        AlbumProductSnapshot.album_product_id
                        == initial.album_product_id,
                    )
                    .order_by(AlbumProductSnapshot.snapshot_version)
                )
            ).all()
        )
        stored_binding = await session.get(ProductCodeBindingRecord, binding_id)
        outbox = await session.get(OutboxEvent, request_id)
    assert [snapshot.snapshot_version for snapshot in snapshots] == [1, 2]
    assert stored_binding is not None
    assert stored_binding.product_code == "SYNTHETIC-001"
    assert outbox is not None
    assert outbox.aggregate_id == snapshots[1].id
    assert outbox.sequence == 2


@pytest.mark.asyncio
async def test_confirms_two_registered_images_and_replays_exactly(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_evidence_group(auth_session_factory)
    request_id = uuid4()
    service = AlbumProductGroupConfirmationService(
        auth_session_factory,
        clock=lambda: NOW,
    )

    first = await service.confirm(
        opc_id=OPC_ID,
        user_id=seeded.user_id,
        confirmation_request_id=request_id,
        evidence_asset_ids=seeded.evidence_ids,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        level_1_candidate=Level1Category.NAN_HONG,
        level_2_candidate=Level2Category.BRACELET,
    )
    replay = await service.confirm(
        opc_id=OPC_ID,
        user_id=seeded.user_id,
        confirmation_request_id=request_id,
        evidence_asset_ids=seeded.evidence_ids,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        level_1_candidate=Level1Category.NAN_HONG,
        level_2_candidate=Level2Category.BRACELET,
    )

    assert replay == first
    assert first.confirmation_event_id == request_id
    assert first.album_product_id == f"album-product-{request_id}"
    assert first.snapshot_version == 1
    assert first.evidence_asset_ids == seeded.evidence_ids
    async with auth_session_factory() as session:
        snapshot = await session.scalar(
            select(AlbumProductSnapshot).where(
                AlbumProductSnapshot.source_event_id == request_id
            )
        )
        outbox = await session.get(OutboxEvent, request_id)
        snapshot_count = await session.scalar(
            select(func.count(AlbumProductSnapshot.id))
        )
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.topic == "album.product.snapshot.confirmed.v1"
            )
        )
    assert snapshot is not None
    assert outbox is not None
    event = ConfirmedSnapshot.model_validate(snapshot.source_payload)
    batch = ConfirmedSnapshotBatch.model_validate(outbox.payload)
    assert batch.data_classification == "opc_owned_product_material_no_customer_data"
    assert batch.events == (event,)
    assert event.event_id == request_id
    assert event.opc_id == OPC_ID
    assert event.album_product_id == first.album_product_id
    assert event.source_import_task_id == str(seeded.task_id)
    assert event.human_confirmation.confirmed_by == f"user:{seeded.user_id}"
    assert event.human_confirmation.confirmed_at == NOW
    assert event.source_type is ProductSourceType.FIRST_SALE_BEAD
    assert event.mall_product_id is None
    assert event.bead_id is None
    assert event.allowed_consumers == ("categorization", "mall_draft_export")
    assert {image.asset_id for image in event.images}
    assert snapshot.evidence_resolution_mode == "registry_required"
    assert snapshot_count == 1
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_same_confirmation_key_with_different_group_is_conflict(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_evidence_group(auth_session_factory, count=3)
    request_id = uuid4()
    service = AlbumProductGroupConfirmationService(
        auth_session_factory,
        clock=lambda: NOW,
    )
    await service.confirm(
        opc_id=OPC_ID,
        user_id=seeded.user_id,
        confirmation_request_id=request_id,
        evidence_asset_ids=seeded.evidence_ids[:2],
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        level_1_candidate=Level1Category.NAN_HONG,
        level_2_candidate=Level2Category.BRACELET,
    )

    with pytest.raises(EvidenceGroupConflict, match="^group_confirmation_conflict$"):
        await service.confirm(
            opc_id=OPC_ID,
            user_id=seeded.user_id,
            confirmation_request_id=request_id,
            evidence_asset_ids=seeded.evidence_ids[1:],
            source_type=ProductSourceType.LEGACY_MATERIAL,
            level_1_candidate=Level1Category.OTHER,
            level_2_candidate=Level2Category.OTHER,
        )

    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(AlbumProductSnapshot.id))) == 1
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic == "album.product.snapshot.confirmed.v1"
                )
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ("cross_opc", "source_object_key", "cross_task"),
)
async def test_unavailable_or_mixed_evidence_group_is_tenant_neutral(
    auth_session_factory: async_sessionmaker[AsyncSession],
    boundary: str,
) -> None:
    seeded = await _seed_evidence_group(auth_session_factory)
    async with auth_session_factory() as session:
        evidence = await session.get(OpcEvidenceAsset, seeded.evidence_ids[1])
        assert evidence is not None
        if boundary == "cross_opc":
            session.add(OpcWorkspace(id="opc-group-other", display_name="Other OPC"))
            await session.flush()
            evidence.opc_id = "opc-group-other"
        elif boundary == "source_object_key":
            evidence.source_object_key = f"private/other/{uuid4()}/original"
        else:
            other_task = ImportTask(
                id=uuid4(),
                user_id=seeded.user_id,
                name="Group confirmation fixture",
                status=TaskStatus.UPLOADING,
            )
            session.add(other_task)
            await session.flush()
            asset = await session.get(Asset, evidence.source_asset_id)
            assert asset is not None
            asset.task_id = other_task.id
        await session.commit()

    with pytest.raises(EvidenceGroupNotFound, match="^evidence_group_not_found$"):
        await AlbumProductGroupConfirmationService(
            auth_session_factory,
            clock=lambda: NOW,
        ).confirm(
            opc_id=OPC_ID,
            user_id=seeded.user_id,
            confirmation_request_id=uuid4(),
            evidence_asset_ids=seeded.evidence_ids,
            source_type=ProductSourceType.FIRST_SALE_BEAD,
            level_1_candidate=Level1Category.UNKNOWN,
            level_2_candidate=Level2Category.UNKNOWN,
        )

    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(AlbumProductSnapshot.id))) == 0


@pytest.mark.asyncio
async def test_concurrent_exact_confirmation_has_one_snapshot_and_event(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_evidence_group(auth_session_factory)
    request_id = uuid4()
    async with auth_session_factory() as session:
        await session.execute(
            text(
                "CREATE OR REPLACE FUNCTION test_pause_group_snapshot_insert() "
                "RETURNS trigger AS $$ BEGIN PERFORM pg_sleep(0.15); "
                "RETURN NEW; END $$ LANGUAGE plpgsql"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER test_pause_group_snapshot_insert_trigger "
                "BEFORE INSERT ON album_product_snapshots FOR EACH ROW "
                "EXECUTE FUNCTION test_pause_group_snapshot_insert()"
            )
        )
        await session.commit()

    async def confirm():
        return await AlbumProductGroupConfirmationService(
            auth_session_factory,
            clock=lambda: NOW,
        ).confirm(
            opc_id=OPC_ID,
            user_id=seeded.user_id,
            confirmation_request_id=request_id,
            evidence_asset_ids=seeded.evidence_ids,
            source_type=ProductSourceType.FIRST_SALE_BEAD,
            level_1_candidate=Level1Category.NAN_HONG,
            level_2_candidate=Level2Category.BRACELET,
        )

    try:
        first, second = await asyncio.gather(confirm(), confirm())
    finally:
        async with auth_session_factory() as session:
            await session.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "test_pause_group_snapshot_insert_trigger "
                    "ON album_product_snapshots"
                )
            )
            await session.execute(
                text("DROP FUNCTION IF EXISTS test_pause_group_snapshot_insert()")
            )
            await session.commit()

    assert first == second
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(AlbumProductSnapshot.id))) == 1
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic == "album.product.snapshot.confirmed.v1"
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_cross_opc_reuse_of_confirmation_id_is_tenant_neutral(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_evidence_group(auth_session_factory)
    request_id = uuid4()
    service = AlbumProductGroupConfirmationService(
        auth_session_factory, clock=lambda: NOW
    )
    await service.confirm(
        opc_id=OPC_ID,
        user_id=seeded.user_id,
        confirmation_request_id=request_id,
        evidence_asset_ids=seeded.evidence_ids,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        level_1_candidate=Level1Category.NAN_HONG,
        level_2_candidate=Level2Category.BRACELET,
    )
    async with auth_session_factory() as session:
        session.add(OpcWorkspace(id="opc-group-other", display_name="Other OPC"))
        await session.flush()
        session.add(
            OpcMembership(
                opc_id="opc-group-other",
                user_id=seeded.user_id,
                role="reviewer",
            )
        )
        evidences = list(
            (
                await session.scalars(
                    select(OpcEvidenceAsset).where(
                        OpcEvidenceAsset.id.in_(seeded.evidence_ids)
                    )
                )
            ).all()
        )
        for evidence in evidences:
            evidence.opc_id = "opc-group-other"
        await session.commit()

    with pytest.raises(EvidenceGroupNotFound, match="^evidence_group_not_found$"):
        await service.confirm(
            opc_id="opc-group-other",
            user_id=seeded.user_id,
            confirmation_request_id=request_id,
            evidence_asset_ids=seeded.evidence_ids,
            source_type=ProductSourceType.FIRST_SALE_BEAD,
            level_1_candidate=Level1Category.NAN_HONG,
            level_2_candidate=Level2Category.BRACELET,
        )


@pytest.mark.asyncio
async def test_confirmation_id_collision_with_other_outbox_is_stable_conflict(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_evidence_group(auth_session_factory)
    request_id = uuid4()
    async with auth_session_factory() as session:
        session.add(
            OutboxEvent(
                id=request_id,
                topic="unrelated.test",
                aggregate_id=uuid4(),
                sequence=1,
                payload={"safe": True},
            )
        )
        await session.commit()

    with pytest.raises(EvidenceGroupConflict, match="^group_confirmation_conflict$"):
        await AlbumProductGroupConfirmationService(
            auth_session_factory,
            clock=lambda: NOW,
        ).confirm(
            opc_id=OPC_ID,
            user_id=seeded.user_id,
            confirmation_request_id=request_id,
            evidence_asset_ids=seeded.evidence_ids,
            source_type=ProductSourceType.FIRST_SALE_BEAD,
            level_1_candidate=Level1Category.NAN_HONG,
            level_2_candidate=Level2Category.BRACELET,
        )

    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(AlbumProductSnapshot.id))) == 0


@pytest.mark.asyncio
async def test_confirmed_group_outbox_is_consumed_by_existing_categorizer(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_evidence_group(auth_session_factory)
    request_id = uuid4()
    await AlbumProductGroupConfirmationService(
        auth_session_factory,
        clock=lambda: NOW,
    ).confirm(
        opc_id=OPC_ID,
        user_id=seeded.user_id,
        confirmation_request_id=request_id,
        evidence_asset_ids=seeded.evidence_ids,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        level_1_candidate=Level1Category.NAN_HONG,
        level_2_candidate=Level2Category.BRACELET,
    )
    async with auth_session_factory() as session:
        outbox = await session.get(OutboxEvent, request_id)
        assert outbox is not None
        fields = OutboxPublisher._fields(outbox)

    engine = SeedEchoEngine()
    outcome = await CategorizationStreamHandler(
        auth_session_factory,
        engine,
        clock=lambda: NOW,
    ).handle("group-confirmation-stream-1", fields)

    assert outcome.acknowledge is True
    assert outcome.status == "completed"
    assert len(engine.requests) == 1
    assert engine.requests[0].input_event_id == request_id
    assert engine.requests[0].images[0].storage_ref.startswith("evidence/")
    async with auth_session_factory() as session:
        candidate_outbox = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.topic == RESULT_TOPIC)
        )
    assert candidate_outbox is not None
    result = CategorizationBatchResult.model_validate(candidate_outbox.payload)
    assert len(result.events) == 1
    assert result.events[0].input_event_id == request_id
    assert result.events[0].claims_not_evaluated == (
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    )
    assert result.events[0].mall_product_id is None
    assert result.events[0].bead_id is None
