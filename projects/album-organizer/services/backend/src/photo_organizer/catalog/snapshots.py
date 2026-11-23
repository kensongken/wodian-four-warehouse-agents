from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.contracts import (
    ConfirmedSnapshot,
    ConfirmedSnapshotBatch,
)
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.repositories.unit_of_work import UnitOfWork


class AlbumProductSnapshotConflict(Exception):
    """An immutable Album Product snapshot identity changed content."""


class AlbumProductSnapshotService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def ingest(self, batch: ConfirmedSnapshotBatch) -> None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            lock_keys = {
                lock_key
                for event in batch.events
                for lock_key in (
                    f"album-product-snapshot:event:{event.event_id}",
                    (
                        "album-product-snapshot:version:"
                        f"{event.opc_id}:{event.album_product_id}:"
                        f"{event.snapshot_version}"
                    ),
                )
            }
            for lock_key in sorted(lock_keys):
                await unit_of_work.session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": lock_key},
                )
            for event in batch.events:
                await self._ingest_event(unit_of_work.session, event)
            await unit_of_work.session.flush()

    async def _ingest_event(
        self,
        session: AsyncSession,
        event: ConfirmedSnapshot,
    ) -> None:
        existing = (
            await session.scalars(
                select(AlbumProductSnapshot)
                .where(
                    or_(
                        AlbumProductSnapshot.source_event_id == event.event_id,
                        (
                            (AlbumProductSnapshot.opc_id == event.opc_id)
                            & (
                                AlbumProductSnapshot.album_product_id
                                == event.album_product_id
                            )
                            & (
                                AlbumProductSnapshot.snapshot_version
                                == event.snapshot_version
                            )
                        ),
                    )
                )
                .with_for_update()
            )
        ).all()
        payload = event.model_dump(mode="json")
        if existing:
            if len(existing) != 1 or existing[0].source_payload != payload:
                raise AlbumProductSnapshotConflict("snapshot_identity_conflict")
            return

        session.add(
            AlbumProductSnapshot(
                source_event_id=event.event_id,
                opc_id=event.opc_id,
                album_product_id=event.album_product_id,
                snapshot_version=event.snapshot_version,
                source_import_task_id=event.source_import_task_id,
                source_type=event.source_type.value,
                occurred_at=event.occurred_at,
                group_confirmed_by=event.human_confirmation.confirmed_by,
                group_confirmed_at=event.human_confirmation.confirmed_at,
                consent_basis=event.consent.basis,
                evidence_resolution_mode="registry_required",
                allowed_consumers=[value for value in event.allowed_consumers],
                images=[image.model_dump(mode="json") for image in event.images],
                source_payload=payload,
            )
        )
