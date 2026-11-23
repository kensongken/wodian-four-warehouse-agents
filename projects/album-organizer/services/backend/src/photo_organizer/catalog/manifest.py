from datetime import datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, cast, select, tuple_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from photo_organizer.categorization.contracts import (
    ConfirmedClassificationEvent,
    ConfirmedSnapshot,
)
from photo_organizer.categorization.review import CONFIRMED_TOPIC
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem
from photo_organizer.schemas.product_material_manifest import (
    ProductMaterialClassification,
    ProductMaterialManifestItem,
)


class ProductMaterialManifestService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def list_items(
        self,
        *,
        opc_id: str,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> tuple[ProductMaterialManifestItem, ...]:
        base_query = self._base_query(opc_id)

        items: list[ProductMaterialManifestItem] = []
        scan_before = before
        async with self._session_factory() as session:
            while len(items) < limit:
                query = base_query.limit(limit)
                if scan_before is not None:
                    query = query.where(
                        tuple_(
                            ProductClassification.confirmed_at,
                            ProductClassification.id,
                        )
                        < scan_before
                    )
                rows = (await session.execute(query)).all()
                if not rows:
                    break
                for snapshot, classification, review, outbox in rows:
                    scan_before = (
                        classification.confirmed_at,
                        classification.id,
                    )
                    item = self._item(
                        snapshot,
                        classification,
                        review,
                        outbox,
                    )
                    if item is not None:
                        items.append(item)
                    if len(items) == limit:
                        break
                if len(rows) < limit:
                    break
        return tuple(items)

    async def get_item(
        self,
        *,
        opc_id: str,
        confirmation_event_id: UUID,
    ) -> ProductMaterialManifestItem | None:
        query = self._base_query(opc_id).where(OutboxEvent.id == confirmation_event_id)
        async with self._session_factory() as session:
            row = (await session.execute(query)).one_or_none()
        if row is None:
            return None
        snapshot, classification, review, outbox = row
        return self._item(snapshot, classification, review, outbox)

    @staticmethod
    def _base_query(
        opc_id: str,
    ) -> Select[
        tuple[
            AlbumProductSnapshot,
            ProductClassification,
            ClassificationReviewItem,
            OutboxEvent,
        ]
    ]:
        return (
            select(
                AlbumProductSnapshot,
                ProductClassification,
                ClassificationReviewItem,
                OutboxEvent,
            )
            .join(
                ProductClassification,
                and_(
                    ProductClassification.opc_id == AlbumProductSnapshot.opc_id,
                    ProductClassification.album_product_id
                    == AlbumProductSnapshot.album_product_id,
                    ProductClassification.input_snapshot_version
                    == AlbumProductSnapshot.snapshot_version,
                    ProductClassification.input_event_id
                    == AlbumProductSnapshot.source_event_id,
                ),
            )
            .join(
                ClassificationReviewItem,
                and_(
                    ClassificationReviewItem.opc_id == ProductClassification.opc_id,
                    ClassificationReviewItem.classification_id
                    == ProductClassification.id,
                ),
            )
            .join(
                OutboxEvent,
                and_(
                    OutboxEvent.aggregate_id == ProductClassification.id,
                    OutboxEvent.topic == CONFIRMED_TOPIC,
                    OutboxEvent.sequence == 1,
                ),
            )
            .where(
                AlbumProductSnapshot.opc_id == opc_id,
                ProductClassification.opc_id == opc_id,
                ClassificationReviewItem.opc_id == opc_id,
                ProductClassification.status == "confirmed",
                ClassificationReviewItem.status == "resolved",
                cast(AlbumProductSnapshot.allowed_consumers, JSONB).contains(
                    ["mall_draft_export"]
                ),
            )
            .order_by(
                ProductClassification.confirmed_at.desc(),
                ProductClassification.id.desc(),
            )
        )

    @staticmethod
    def _item(
        snapshot: AlbumProductSnapshot,
        classification: ProductClassification,
        review: ClassificationReviewItem,
        outbox: OutboxEvent,
    ) -> ProductMaterialManifestItem | None:
        try:
            source = ConfirmedSnapshot.model_validate(snapshot.source_payload)
            confirmation = ConfirmedClassificationEvent.model_validate(outbox.payload)
        except ValidationError:
            return None
        expected_decision = {
            "level_1": confirmation.confirmed_level_1.value,
            "level_2": confirmation.confirmed_level_2.value,
        }
        if (
            source.event_id != snapshot.source_event_id
            or source.opc_id != snapshot.opc_id
            or source.album_product_id != snapshot.album_product_id
            or source.snapshot_version != snapshot.snapshot_version
            or source.source_type.value != snapshot.source_type
            or classification.source_type != snapshot.source_type
            or "mall_draft_export" not in source.allowed_consumers
            or confirmation.event_id != outbox.id
            or confirmation.opc_id != snapshot.opc_id
            or confirmation.album_product_id != snapshot.album_product_id
            or confirmation.input_event_id != snapshot.source_event_id
            or confirmation.input_snapshot_version != snapshot.snapshot_version
            or confirmation.classification_id != classification.id
            or confirmation.review_id != review.id
            or confirmation.source_type.value != snapshot.source_type
            or confirmation.classification_version
            != classification.classification_version
            or confirmation.confirmed_level_1.value != classification.confirmed_level_1
            or confirmation.confirmed_level_2.value != classification.confirmed_level_2
            or confirmation.confirmed_by != classification.confirmed_by
            or confirmation.confirmed_at != classification.confirmed_at
            or review.decision_payload != expected_decision
            or review.decided_by != confirmation.confirmed_by
            or review.decided_at != confirmation.confirmed_at
        ):
            return None
        return ProductMaterialManifestItem(
            album_product_id=source.album_product_id,
            snapshot_version=source.snapshot_version,
            source_type=source.source_type,
            source_event_id=source.event_id,
            source_import_task_id=source.source_import_task_id,
            images=source.images,
            classification=ProductMaterialClassification(
                classification_id=classification.id,
                review_id=review.id,
                input_snapshot_version=classification.input_snapshot_version,
                classification_version=classification.classification_version,
                confirmed_level_1=confirmation.confirmed_level_1,
                confirmed_level_2=confirmation.confirmed_level_2,
                confirmed_by=confirmation.confirmed_by,
                confirmed_at=confirmation.confirmed_at,
            ),
            confirmation_event_id=confirmation.event_id,
            confirmation_schema_version=confirmation.schema_version,
            claims_not_evaluated=confirmation.claims_not_evaluated,
            mall_product_id=None,
            bead_id=None,
            publication_mode="draft_only",
            auto_publish=False,
        )
