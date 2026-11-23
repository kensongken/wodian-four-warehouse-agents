from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fakes.fake_object_storage import FakeObjectStorage
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.downloads import (
    MaterialNotFound,
    MaterialTemporarilyUnavailable,
    MaterialUnavailable,
    ProductMaterialDownloadService,
)
from photo_organizer.catalog.snapshots import AlbumProductSnapshotService
from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    ConfirmedSnapshotBatch,
    ImageEvidence,
)
from photo_organizer.categorization.review import ClassificationReviewService
from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.opc import OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User
from photo_organizer.storage.object_storage import (
    StorageNotFound,
    StoragePermanent,
    StorageTransient,
)

ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_FIXTURE = ROOT / "contracts/fixtures/first-10-sku-categorization-events.json"
CANDIDATE_FIXTURE = (
    ROOT / "contracts/fixtures/first-10-sku-classification-candidates.json"
)
OPC_ID = "synthetic-opc-001"
NOW = datetime(2026, 8, 17, 11, tzinfo=UTC)


def _snapshot_batch(*, storage_ref: str | None = None) -> ConfirmedSnapshotBatch:
    source = ConfirmedSnapshotBatch.model_validate_json(
        SNAPSHOT_FIXTURE.read_text(encoding="utf-8")
    )
    batch = ConfirmedSnapshotBatch(
        schema_version=source.schema_version,
        batch_id=source.batch_id,
        generated_at=source.generated_at,
        data_classification=source.data_classification,
        events=(source.events[0],),
    )
    if storage_ref is None:
        return batch
    payload = batch.model_dump(mode="json")
    payload["events"][0]["images"][0]["storage_ref"] = storage_ref
    return ConfirmedSnapshotBatch.model_validate(payload)


def _candidate_batch() -> CategorizationBatchResult:
    source = CategorizationBatchResult.model_validate_json(
        CANDIDATE_FIXTURE.read_text(encoding="utf-8")
    )
    return CategorizationBatchResult(
        schema_version=source.schema_version,
        input_batch_id=source.input_batch_id,
        generated_at=source.generated_at,
        events=(source.events[0],),
        failures=(),
    )


@pytest_asyncio.fixture(autouse=True)
async def clear_material_download_records(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async def clear() -> None:
        async with auth_session_factory() as session:
            await session.execute(delete(OpcEvidenceAsset))
            await session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.topic == "album.product.classification.confirmed.v1"
                )
            )
            await session.execute(delete(ClassificationReviewItem))
            await session.execute(delete(ProductClassification))
            await session.execute(delete(AlbumProductSnapshot))
            await session.execute(
                delete(OpcWorkspace).where(
                    OpcWorkspace.id.in_((OPC_ID, "synthetic-opc-other"))
                )
            )
            await session.commit()

    await clear()
    yield
    await clear()


async def _seed_confirmed_material(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshots: ConfirmedSnapshotBatch | None = None,
) -> tuple[UUID, UUID, str]:
    snapshots = snapshots or _snapshot_batch()
    candidates = _candidate_batch()
    await AlbumProductSnapshotService(session_factory).ingest(snapshots)
    reviews = await ClassificationReviewService(session_factory).ingest(candidates)
    classification = await ClassificationReviewService(
        session_factory,
        clock=lambda: NOW,
    ).confirm(
        opc_id=OPC_ID,
        review_id=reviews[0].id,
        level_1=candidates.events[0].candidates[0].level_1.value,
        level_2=candidates.events[0].candidates[0].level_2.value,
        confirmed_by="opc-reviewer-001",
    )
    async with session_factory() as session:
        confirmation_event_id = await session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.aggregate_id == classification.id,
                OutboxEvent.topic == "album.product.classification.confirmed.v1",
            )
        )
    assert confirmation_event_id is not None
    image = snapshots.events[0].images[0]
    return confirmation_event_id, image.asset_id, image.storage_ref


async def _seed_registered_material(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, ImageEvidence, str, UUID]:
    evidence_id = uuid4()
    storage_ref = f"evidence/{evidence_id}/original"
    snapshots = _snapshot_batch(storage_ref=storage_ref)
    confirmation_event_id, _, _ = await _seed_confirmed_material(
        session_factory,
        snapshots=snapshots,
    )
    image = snapshots.events[0].images[0]
    user = User(id=uuid4(), phone_e164=f"+86136{str(uuid4().int)[-8:]}")
    task = ImportTask(
        id=uuid4(),
        user_id=user.id,
        name="Registered material",
        status=TaskStatus.UPLOADING,
    )
    source_object_key = f"private/{user.id}/{image.asset_id}/original"
    blob = StoredBlob(
        id=uuid4(),
        user_id=user.id,
        sha256=image.sha256,
        byte_size=image.byte_size,
        object_key=source_object_key,
        is_ready=True,
    )
    asset = Asset(
        id=image.asset_id,
        user_id=user.id,
        task_id=task.id,
        blob_id=blob.id,
        local_asset_id=f"registered-{image.asset_id}",
        filename="registered-original.jpg",
        media_type=image.media_type,
        byte_size=image.byte_size,
        sha256=image.sha256,
        captured_at=image.captured_at,
        status=AssetStatus.UPLOADED,
        created_at=image.imported_at,
        updated_at=image.imported_at,
    )
    evidence = OpcEvidenceAsset(
        id=evidence_id,
        opc_id=OPC_ID,
        source_asset_id=asset.id,
        source_blob_id=blob.id,
        created_by=user.id,
        storage_ref=storage_ref,
        source_object_key=source_object_key,
        sha256=image.sha256,
        byte_size=image.byte_size,
        media_type=image.media_type,
        captured_at=image.captured_at,
        imported_at=image.imported_at,
    )
    async with session_factory() as session:
        session.add_all(
            [
                user,
                OpcWorkspace(id=OPC_ID, display_name="Registered Material OPC"),
            ]
        )
        await session.flush()
        session.add_all([task, blob])
        await session.flush()
        session.add(asset)
        await session.flush()
        session.add(evidence)
        await session.commit()
    return confirmation_event_id, image, source_object_key, evidence.id


@pytest.mark.asyncio
async def test_legacy_direct_snapshot_without_registry_mapping_still_signs(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    confirmation_event_id, asset_id, storage_ref = await _seed_confirmed_material(
        auth_session_factory
    )
    async with auth_session_factory() as session:
        snapshot = await session.scalar(select(AlbumProductSnapshot))
        assert snapshot is not None
        snapshot.evidence_resolution_mode = "legacy_direct"
        await session.commit()
    storage = FakeObjectStorage()
    service = ProductMaterialDownloadService(auth_session_factory, storage)

    result = await service.sign(
        opc_id=OPC_ID,
        confirmation_event_id=confirmation_event_id,
        asset_id=asset_id,
    )

    assert result.expires_seconds == 300
    assert result.download_url.endswith("X-Amz-Signature=fake-download-signature")
    assert storage.download_sign_calls == [storage_ref]


@pytest.mark.asyncio
async def test_registry_required_snapshot_without_mapping_fails_closed(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    confirmation_event_id, asset_id, _ = await _seed_confirmed_material(
        auth_session_factory
    )
    storage = FakeObjectStorage()

    with pytest.raises(MaterialNotFound, match="^material_not_found$"):
        await ProductMaterialDownloadService(
            auth_session_factory,
            storage,
        ).sign(
            opc_id=OPC_ID,
            confirmation_event_id=confirmation_event_id,
            asset_id=asset_id,
        )

    assert storage.download_sign_call_count == 0


@pytest.mark.asyncio
async def test_registered_evidence_signs_private_source_object_key(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    (
        confirmation_event_id,
        image,
        source_object_key,
        _,
    ) = await _seed_registered_material(auth_session_factory)
    storage = FakeObjectStorage()

    result = await ProductMaterialDownloadService(
        auth_session_factory,
        storage,
    ).sign(
        opc_id=OPC_ID,
        confirmation_event_id=confirmation_event_id,
        asset_id=image.asset_id,
    )

    assert result.expires_seconds == 300
    assert storage.download_sign_calls == [source_object_key]
    assert image.storage_ref not in result.download_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "opc_id",
        "source_asset_id",
        "storage_ref",
        "sha256",
        "byte_size",
        "media_type",
        "captured_at",
        "imported_at",
        "source_object_key",
        "source_blob_id",
        "created_by",
    ),
)
async def test_registered_evidence_mismatch_never_falls_back_to_logical_ref(
    auth_session_factory: async_sessionmaker[AsyncSession],
    boundary: str,
) -> None:
    confirmation_event_id, image, _, evidence_id = await _seed_registered_material(
        auth_session_factory
    )
    async with auth_session_factory() as session:
        evidence = await session.get(OpcEvidenceAsset, evidence_id)
        assert evidence is not None
        if boundary == "opc_id":
            session.add(
                OpcWorkspace(
                    id="synthetic-opc-other",
                    display_name="Other Evidence OPC",
                )
            )
            await session.flush()
            evidence.opc_id = "synthetic-opc-other"
        elif boundary == "source_asset_id":
            replacement = Asset(
                id=uuid4(),
                user_id=evidence.created_by,
                task_id=(await session.get(Asset, image.asset_id)).task_id,  # type: ignore[union-attr]
                blob_id=evidence.source_blob_id,
                local_asset_id=f"replacement-{uuid4()}",
                filename="replacement.jpg",
                media_type=image.media_type,
                byte_size=image.byte_size,
                sha256=image.sha256,
                status=AssetStatus.UPLOADED,
            )
            session.add(replacement)
            await session.flush()
            evidence.source_asset_id = replacement.id
        elif boundary == "storage_ref":
            evidence.storage_ref = f"evidence/{uuid4()}/original"
        elif boundary == "sha256":
            evidence.sha256 = "f" * 64
        elif boundary == "byte_size":
            evidence.byte_size += 1
        elif boundary == "media_type":
            evidence.media_type = "image/png"
        elif boundary == "captured_at":
            evidence.captured_at = datetime(2026, 8, 17, 18, tzinfo=UTC)
        elif boundary == "imported_at":
            evidence.imported_at = datetime(2026, 8, 17, 19, tzinfo=UTC)
        elif boundary == "source_object_key":
            evidence.source_object_key = f"private/other-opc/{uuid4()}/original"
        elif boundary == "source_blob_id":
            alternate_blob = StoredBlob(
                id=uuid4(),
                user_id=evidence.created_by,
                sha256="e" * 64,
                byte_size=image.byte_size,
                object_key=f"private/{evidence.created_by}/{uuid4()}/original",
                is_ready=True,
            )
            session.add(alternate_blob)
            await session.flush()
            evidence.source_blob_id = alternate_blob.id
        else:
            other_user = User(
                id=uuid4(),
                phone_e164=f"+86135{str(uuid4().int)[-8:]}",
            )
            session.add(other_user)
            await session.flush()
            evidence.created_by = other_user.id
        await session.commit()
    storage = FakeObjectStorage()

    with pytest.raises(MaterialNotFound, match="^material_not_found$"):
        await ProductMaterialDownloadService(
            auth_session_factory,
            storage,
        ).sign(
            opc_id=OPC_ID,
            confirmation_event_id=confirmation_event_id,
            asset_id=image.asset_id,
        )

    assert storage.download_sign_call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "wrong_opc",
        "unknown_event",
        "unknown_asset",
        "mismatched_review",
        "consumer_not_allowed",
    ),
)
async def test_invalid_manifest_boundaries_never_reach_storage(
    auth_session_factory: async_sessionmaker[AsyncSession],
    boundary: str,
) -> None:
    confirmation_event_id, asset_id, _ = await _seed_confirmed_material(
        auth_session_factory
    )
    opc_id = OPC_ID
    if boundary == "wrong_opc":
        opc_id = "synthetic-opc-other"
    elif boundary == "unknown_event":
        confirmation_event_id = uuid4()
    elif boundary == "unknown_asset":
        asset_id = uuid4()
    elif boundary == "mismatched_review":
        async with auth_session_factory() as session:
            review = await session.scalar(select(ClassificationReviewItem))
            assert review is not None
            review.decided_by = "opc-reviewer-other"
            await session.commit()
    else:
        async with auth_session_factory() as session:
            snapshot = await session.scalar(select(AlbumProductSnapshot))
            assert snapshot is not None
            snapshot.allowed_consumers = ["categorization"]
            await session.commit()

    storage = FakeObjectStorage()
    service = ProductMaterialDownloadService(auth_session_factory, storage)

    with pytest.raises(MaterialNotFound, match="^material_not_found$"):
        await service.sign(
            opc_id=opc_id,
            confirmation_event_id=confirmation_event_id,
            asset_id=asset_id,
        )

    assert storage.download_sign_call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("storage_error", "expected"),
    (
        (StorageNotFound, MaterialNotFound),
        (StorageTransient, MaterialTemporarilyUnavailable),
        (StoragePermanent, MaterialUnavailable),
    ),
)
async def test_normalizes_download_storage_failures(
    auth_session_factory: async_sessionmaker[AsyncSession],
    storage_error: type[Exception],
    expected: type[Exception],
) -> None:
    confirmation_event_id, image, _, _ = await _seed_registered_material(
        auth_session_factory
    )
    storage = FakeObjectStorage()
    storage.inject_fault("sign_download", storage_error)
    service = ProductMaterialDownloadService(auth_session_factory, storage)

    with pytest.raises(expected) as raised:
        await service.sign(
            opc_id=OPC_ID,
            confirmation_event_id=confirmation_event_id,
            asset_id=image.asset_id,
        )

    assert str(raised.value) in {
        "material_not_found",
        "material_temporarily_unavailable",
        "material_unavailable",
    }
    assert "storage_injected" not in str(raised.value)
