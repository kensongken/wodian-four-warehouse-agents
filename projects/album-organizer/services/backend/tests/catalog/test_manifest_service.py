from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.manifest import ProductMaterialManifestService
from photo_organizer.catalog.snapshots import AlbumProductSnapshotService
from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    ConfirmedSnapshot,
    ConfirmedSnapshotBatch,
    ImageEvidence,
)
from photo_organizer.categorization.review import ClassificationReviewService
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem

ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_FIXTURE = ROOT / "contracts/fixtures/first-10-sku-categorization-events.json"
CANDIDATE_FIXTURE = (
    ROOT / "contracts/fixtures/first-10-sku-classification-candidates.json"
)
NOW = datetime(2026, 8, 17, 9, tzinfo=UTC)


def _snapshot_batch(indices: tuple[int, ...]) -> ConfirmedSnapshotBatch:
    batch = ConfirmedSnapshotBatch.model_validate_json(
        SNAPSHOT_FIXTURE.read_text(encoding="utf-8")
    )
    return ConfirmedSnapshotBatch(
        schema_version=batch.schema_version,
        batch_id=batch.batch_id,
        generated_at=batch.generated_at,
        data_classification=batch.data_classification,
        events=tuple(batch.events[index] for index in indices),
    )


def _candidate_batch(indices: tuple[int, ...]) -> CategorizationBatchResult:
    batch = CategorizationBatchResult.model_validate_json(
        CANDIDATE_FIXTURE.read_text(encoding="utf-8")
    )
    return CategorizationBatchResult(
        schema_version=batch.schema_version,
        input_batch_id=batch.input_batch_id,
        generated_at=batch.generated_at,
        events=tuple(batch.events[index] for index in indices),
        failures=(),
    )


@pytest_asyncio.fixture(autouse=True)
async def clear_catalog_records(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with auth_session_factory() as session:
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        await session.execute(delete(ClassificationReviewItem))
        await session.execute(delete(ProductClassification))
        await session.execute(delete(AlbumProductSnapshot))
        await session.commit()
    yield
    async with auth_session_factory() as session:
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        await session.execute(delete(ClassificationReviewItem))
        await session.execute(delete(ProductClassification))
        await session.execute(delete(AlbumProductSnapshot))
        await session.commit()


async def _ingest_and_confirm(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot_batch: ConfirmedSnapshotBatch,
    candidate_batch: CategorizationBatchResult,
    confirm_indices: tuple[int, ...],
) -> tuple[UUID, ...]:
    await AlbumProductSnapshotService(session_factory).ingest(snapshot_batch)
    reviews = await ClassificationReviewService(
        session_factory,
        clock=lambda: NOW,
    ).ingest(candidate_batch)
    for index in confirm_indices:
        candidate = candidate_batch.events[index]
        await ClassificationReviewService(
            session_factory,
            clock=lambda: NOW,
        ).confirm(
            opc_id=candidate.opc_id,
            review_id=reviews[index].id,
            level_1=candidate.candidates[0].level_1.value,
            level_2=candidate.candidates[0].level_2.value,
            confirmed_by="opc-reviewer-001",
        )
    return tuple(review.id for review in reviews)


@pytest.mark.asyncio
async def test_manifest_returns_only_confirmed_mall_draft_authorized_snapshots(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    source_payload = _snapshot_batch((0, 1, 2)).model_dump(mode="json")
    source_payload["events"][2]["allowed_consumers"] = ["categorization"]
    snapshots = ConfirmedSnapshotBatch.model_validate(source_payload)
    candidates = _candidate_batch((0, 1, 2))
    await _ingest_and_confirm(
        auth_session_factory,
        snapshot_batch=snapshots,
        candidate_batch=candidates,
        confirm_indices=(0, 2),
    )

    items = await ProductMaterialManifestService(auth_session_factory).list_items(
        opc_id="synthetic-opc-001",
        limit=20,
        before=None,
    )

    assert len(items) == 1
    item = items[0]
    assert item.album_product_id == "album-product-synthetic-001"
    assert item.snapshot_version == 1
    assert item.source_type.value == "first_sale_bead"
    assert item.classification.confirmed_level_1.value == "南红"
    assert item.classification.confirmed_level_2.value == "手串"
    assert item.confirmation_schema_version == (
        "album-product-classification-confirmed/1.0"
    )
    assert (
        tuple(
            ImageEvidence.model_validate(image.model_dump(mode="json"))
            for image in item.images
        )
        == item.images
    )
    assert item.claims_not_evaluated == (
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    )
    assert item.mall_product_id is None
    assert item.bead_id is None
    assert item.publication_mode == "draft_only"
    assert item.auto_publish is False


@pytest.mark.asyncio
async def test_manifest_is_tenant_scoped_and_omits_mismatched_confirmation_event(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshots = _snapshot_batch((0,))
    candidates = _candidate_batch((0,))
    await _ingest_and_confirm(
        auth_session_factory,
        snapshot_batch=snapshots,
        candidate_batch=candidates,
        confirm_indices=(0,),
    )
    async with auth_session_factory() as session:
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        assert event is not None
        changed = dict(event.payload)
        changed["album_product_id"] = "album-product-wrong"
        event.payload = changed
        await session.commit()

    service = ProductMaterialManifestService(auth_session_factory)

    assert (
        await service.list_items(
            opc_id="opc-other",
            limit=20,
            before=None,
        )
        == ()
    )
    assert (
        await service.list_items(
            opc_id="synthetic-opc-001",
            limit=20,
            before=None,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_manifest_scans_past_invalid_newest_row_to_fill_page(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshots = _snapshot_batch((0, 1))
    candidates = _candidate_batch((0, 1))
    await _ingest_and_confirm(
        auth_session_factory,
        snapshot_batch=snapshots,
        candidate_batch=candidates,
        confirm_indices=(0, 1),
    )
    async with auth_session_factory() as session:
        classifications = (
            await session.scalars(
                select(ProductClassification).order_by(
                    ProductClassification.confirmed_at.desc(),
                    ProductClassification.id.desc(),
                )
            )
        ).all()
        assert len(classifications) == 2
        newest = classifications[0]
        newest_event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == newest.id,
                OutboxEvent.topic == "album.product.classification.confirmed.v1",
            )
        )
        assert newest_event is not None
        changed = dict(newest_event.payload)
        changed["album_product_id"] = "album-product-wrong"
        newest_event.payload = changed
        await session.commit()

    items = await ProductMaterialManifestService(auth_session_factory).list_items(
        opc_id="synthetic-opc-001",
        limit=1,
        before=None,
    )

    assert len(items) == 1
    assert items[0].classification.classification_id == classifications[1].id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    (
        "classification_source_type",
        "review_decision_payload",
        "review_decided_by",
        "review_decided_at",
    ),
)
async def test_manifest_omits_inconsistent_durable_confirmation_chain(
    auth_session_factory: async_sessionmaker[AsyncSession],
    mismatch: str,
) -> None:
    snapshots = _snapshot_batch((0,))
    candidates = _candidate_batch((0,))
    await _ingest_and_confirm(
        auth_session_factory,
        snapshot_batch=snapshots,
        candidate_batch=candidates,
        confirm_indices=(0,),
    )
    async with auth_session_factory() as session:
        classification = await session.scalar(select(ProductClassification))
        review = await session.scalar(select(ClassificationReviewItem))
        assert classification is not None
        assert review is not None
        if mismatch == "classification_source_type":
            classification.source_type = "legacy_material"
        elif mismatch == "review_decision_payload":
            review.decision_payload = {
                "level_1": "沉香",
                "level_2": "手串",
            }
        elif mismatch == "review_decided_by":
            review.decided_by = "opc-reviewer-other"
        else:
            assert review.decided_at is not None
            review.decided_at += timedelta(seconds=1)
        await session.commit()

    items = await ProductMaterialManifestService(auth_session_factory).list_items(
        opc_id="synthetic-opc-001",
        limit=20,
        before=None,
    )

    assert items == ()


@pytest.mark.asyncio
async def test_manifest_keeps_multiple_confirmed_snapshot_versions(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshots_v1 = _snapshot_batch((0,))
    candidates_v1 = _candidate_batch((0,))
    await _ingest_and_confirm(
        auth_session_factory,
        snapshot_batch=snapshots_v1,
        candidate_batch=candidates_v1,
        confirm_indices=(0,),
    )

    source = snapshots_v1.events[0].model_dump(mode="json")
    source["event_id"] = "20000000-0000-4000-8000-000000000099"
    source["snapshot_version"] = 2
    source["images"][0]["asset_id"] = "30000000-0000-4000-8000-000000000099"
    source["images"][0]["sha256"] = "9" * 64
    source["images"][1]["asset_id"] = "30000000-0000-4000-8000-000000000098"
    source["images"][1]["sha256"] = "8" * 64
    source["images"][1]["source_asset_id"] = source["images"][0]["asset_id"]
    snapshot_v2 = ConfirmedSnapshot.model_validate(source)

    candidate = candidates_v1.events[0].model_dump(mode="json")
    candidate["event_id"] = "a09f4c9f-f2c0-507f-a524-516eaf235499"
    candidate["input_event_id"] = str(snapshot_v2.event_id)
    candidate["input_snapshot_version"] = 2
    candidates_v2 = CategorizationBatchResult.model_validate(
        {
            **candidates_v1.model_dump(mode="json"),
            "events": [candidate],
        }
    )
    snapshots_v2 = ConfirmedSnapshotBatch(
        schema_version=snapshots_v1.schema_version,
        batch_id=snapshots_v1.batch_id,
        generated_at=snapshots_v1.generated_at,
        data_classification=snapshots_v1.data_classification,
        events=(snapshot_v2,),
    )
    await _ingest_and_confirm(
        auth_session_factory,
        snapshot_batch=snapshots_v2,
        candidate_batch=candidates_v2,
        confirm_indices=(0,),
    )

    items = await ProductMaterialManifestService(auth_session_factory).list_items(
        opc_id="synthetic-opc-001",
        limit=20,
        before=None,
    )

    assert {item.snapshot_version for item in items} == {1, 2}
    assert len({item.confirmation_event_id for item in items}) == 2
