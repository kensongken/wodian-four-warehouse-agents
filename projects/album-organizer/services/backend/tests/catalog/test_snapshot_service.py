from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.snapshots import (
    AlbumProductSnapshotConflict,
    AlbumProductSnapshotService,
)
from photo_organizer.categorization.contracts import (
    ConfirmedSnapshot,
    ConfirmedSnapshotBatch,
)
from photo_organizer.models.album_product import AlbumProductSnapshot

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "contracts/fixtures/first-10-sku-categorization-events.json"
)


def _batch_payload() -> dict[str, object]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _batch() -> ConfirmedSnapshotBatch:
    return ConfirmedSnapshotBatch.model_validate(_batch_payload())


def _changed_batch(
    batch: ConfirmedSnapshotBatch,
    *,
    event_index: int = 0,
) -> ConfirmedSnapshotBatch:
    payload = batch.model_dump(mode="json")
    event = payload["events"][event_index]
    event["images"][0]["sha256"] = "f" * 64
    return ConfirmedSnapshotBatch.model_validate(payload)


@pytest_asyncio.fixture(autouse=True)
async def clear_snapshots(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with auth_session_factory() as session:
        await session.execute(delete(AlbumProductSnapshot))
        await session.commit()
    yield
    async with auth_session_factory() as session:
        await session.execute(delete(AlbumProductSnapshot))
        await session.commit()


@pytest.mark.asyncio
async def test_ingest_persists_ten_immutable_snapshots_atomically(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch = _batch()

    await AlbumProductSnapshotService(auth_session_factory).ingest(batch)

    async with auth_session_factory() as session:
        rows = (
            await session.scalars(
                select(AlbumProductSnapshot).order_by(
                    AlbumProductSnapshot.album_product_id
                )
            )
        ).all()
    assert len(rows) == 10
    first = rows[0]
    source = ConfirmedSnapshot.model_validate(first.source_payload)
    assert first.source_event_id == source.event_id
    assert first.opc_id == source.opc_id
    assert first.album_product_id == source.album_product_id
    assert first.snapshot_version == 1
    assert len(first.images) == 2
    assert source.images[0].storage_ref.startswith("evidence/")
    assert first.allowed_consumers == ["categorization", "mall_draft_export"]


@pytest.mark.asyncio
async def test_exact_replay_is_idempotent_but_changed_identity_is_rejected(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = AlbumProductSnapshotService(auth_session_factory)
    batch = _batch()
    await service.ingest(batch)

    await service.ingest(batch)
    with pytest.raises(
        AlbumProductSnapshotConflict,
        match="^snapshot_identity_conflict$",
    ):
        await service.ingest(_changed_batch(batch))

    async with auth_session_factory() as session:
        count = await session.scalar(select(func.count(AlbumProductSnapshot.id)))
        persisted = await session.scalar(
            select(AlbumProductSnapshot).where(
                AlbumProductSnapshot.source_event_id == batch.events[0].event_id
            )
        )
    assert count == 10
    assert persisted is not None
    assert persisted.source_payload == batch.events[0].model_dump(mode="json")


@pytest.mark.asyncio
async def test_conflicting_last_item_rolls_back_all_new_rows_in_the_batch(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch = _batch()
    service = AlbumProductSnapshotService(auth_session_factory)
    last_only = ConfirmedSnapshotBatch(
        schema_version=batch.schema_version,
        batch_id=batch.batch_id,
        generated_at=batch.generated_at,
        data_classification=batch.data_classification,
        events=(batch.events[-1],),
    )
    await service.ingest(last_only)

    with pytest.raises(
        AlbumProductSnapshotConflict,
        match="^snapshot_identity_conflict$",
    ):
        await service.ingest(_changed_batch(batch, event_index=9))

    async with auth_session_factory() as session:
        rows = (await session.scalars(select(AlbumProductSnapshot))).all()
    assert len(rows) == 1
    assert rows[0].source_event_id == batch.events[-1].event_id


@pytest.mark.asyncio
async def test_cross_opc_concurrent_source_event_conflict_is_stable(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = _batch().events[0]
    changed_payload = original.model_dump(mode="json")
    changed_payload["opc_id"] = "synthetic-opc-other"
    changed_payload["album_product_id"] = "album-product-cross-opc"
    changed = ConfirmedSnapshot.model_validate(changed_payload)

    def one_event_batch(event: ConfirmedSnapshot) -> ConfirmedSnapshotBatch:
        source = _batch()
        return ConfirmedSnapshotBatch(
            schema_version=source.schema_version,
            batch_id=source.batch_id,
            generated_at=source.generated_at,
            data_classification=source.data_classification,
            events=(event,),
        )

    async with auth_session_factory() as session:
        await session.execute(
            text(
                "DROP TRIGGER IF EXISTS test_delay_album_product_insert "
                "ON album_product_snapshots"
            )
        )
        await session.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION test_delay_album_product_insert()
                RETURNS trigger AS $$
                BEGIN
                    PERFORM pg_sleep(0.2);
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        await session.execute(
            text(
                """
                CREATE TRIGGER test_delay_album_product_insert
                BEFORE INSERT ON album_product_snapshots
                FOR EACH ROW EXECUTE FUNCTION test_delay_album_product_insert()
                """
            )
        )
        await session.commit()

    try:
        results = await asyncio.gather(
            AlbumProductSnapshotService(auth_session_factory).ingest(
                one_event_batch(original)
            ),
            AlbumProductSnapshotService(auth_session_factory).ingest(
                one_event_batch(changed)
            ),
            return_exceptions=True,
        )
    finally:
        async with auth_session_factory() as session:
            await session.execute(
                text(
                    "DROP TRIGGER IF EXISTS test_delay_album_product_insert "
                    "ON album_product_snapshots"
                )
            )
            await session.execute(
                text("DROP FUNCTION IF EXISTS test_delay_album_product_insert()")
            )
            await session.commit()

    assert sum(result is None for result in results) == 1
    conflicts = [
        result for result in results if isinstance(result, AlbumProductSnapshotConflict)
    ]
    assert len(conflicts) == 1
    assert str(conflicts[0]) == "snapshot_identity_conflict"
    assert not [
        result
        for result in results
        if isinstance(result, BaseException)
        and not isinstance(result, AlbumProductSnapshotConflict)
    ]
