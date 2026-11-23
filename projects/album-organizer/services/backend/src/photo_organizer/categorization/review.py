from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    ClassificationCandidateEvent,
    ConfirmedClassificationEvent,
    Level1Category,
    Level2Category,
)
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem
from photo_organizer.repositories.unit_of_work import UnitOfWork

CONFIRMED_TOPIC: Literal["album.product.classification.confirmed.v1"] = (
    "album.product.classification.confirmed.v1"
)


class ClassificationReviewError(Exception):
    """Base class for expected classification review failures."""


class ClassificationReviewNotFound(ClassificationReviewError):
    """The tenant-scoped review item does not exist."""


class ClassificationReviewConflict(ClassificationReviewError):
    """The requested write would replace immutable review history."""


class ClassificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level_1: Level1Category
    level_2: Level2Category
    confirmed_by: Annotated[
        str,
        Field(
            min_length=3,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ]


@dataclass(frozen=True)
class ClassificationReviewRecord:
    review: ClassificationReviewItem
    classification: ProductClassification


class ClassificationReviewService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def list_reviews(
        self,
        *,
        opc_id: str,
        review_status: Literal["open", "resolved"],
        limit: int,
        before: tuple[datetime, UUID] | None = None,
    ) -> tuple[ClassificationReviewRecord, ...]:
        query = (
            select(ClassificationReviewItem, ProductClassification)
            .join(
                ProductClassification,
                ClassificationReviewItem.classification_id == ProductClassification.id,
            )
            .where(
                ClassificationReviewItem.opc_id == opc_id,
                ProductClassification.opc_id == opc_id,
                ClassificationReviewItem.status == review_status,
            )
            .order_by(
                ClassificationReviewItem.created_at.desc(),
                ClassificationReviewItem.id.desc(),
            )
            .limit(limit)
        )
        if before is not None:
            query = query.where(
                tuple_(
                    ClassificationReviewItem.created_at,
                    ClassificationReviewItem.id,
                )
                < before
            )
        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        return tuple(
            ClassificationReviewRecord(review=review, classification=classification)
            for review, classification in rows
        )

    async def get_review(
        self,
        *,
        opc_id: str,
        review_id: UUID,
    ) -> ClassificationReviewRecord:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ClassificationReviewItem, ProductClassification)
                    .join(
                        ProductClassification,
                        ClassificationReviewItem.classification_id
                        == ProductClassification.id,
                    )
                    .where(
                        ClassificationReviewItem.id == review_id,
                        ClassificationReviewItem.opc_id == opc_id,
                        ProductClassification.opc_id == opc_id,
                    )
                )
            ).one_or_none()
        if row is None:
            raise ClassificationReviewNotFound("review_not_found")
        review, classification = row
        return ClassificationReviewRecord(
            review=review,
            classification=classification,
        )

    async def ingest(
        self, batch: CategorizationBatchResult
    ) -> tuple[ClassificationReviewItem, ...]:
        reviews: list[ClassificationReviewItem] = []
        async with UnitOfWork(self._session_factory) as unit_of_work:
            for opc_id in sorted({event.opc_id for event in batch.events}):
                await unit_of_work.session.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"
                    ),
                    {"lock_key": f"classification-review:{opc_id}"},
                )
            for event in batch.events:
                reviews.append(await self._ingest_event(unit_of_work.session, event))
        return tuple(reviews)

    async def confirm(
        self,
        *,
        opc_id: str,
        review_id: UUID,
        level_1: str,
        level_2: str,
        confirmed_by: str,
    ) -> ProductClassification:
        decision = ClassificationDecision.model_validate(
            {
                "level_1": level_1,
                "level_2": level_2,
                "confirmed_by": confirmed_by,
            }
        )
        decision_payload: dict[str, object] = {
            "level_1": decision.level_1.value,
            "level_2": decision.level_2.value,
        }
        async with UnitOfWork(self._session_factory) as unit_of_work:
            row = (
                await unit_of_work.session.execute(
                    select(ClassificationReviewItem, ProductClassification)
                    .join(
                        ProductClassification,
                        ClassificationReviewItem.classification_id
                        == ProductClassification.id,
                    )
                    .where(
                        ClassificationReviewItem.id == review_id,
                        ClassificationReviewItem.opc_id == opc_id,
                        ProductClassification.opc_id == opc_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ClassificationReviewNotFound("review_not_found")
            review, classification = row

            if review.status == "resolved":
                if (
                    review.decision_payload == decision_payload
                    and review.decided_by == decision.confirmed_by
                ):
                    return classification
                raise ClassificationReviewConflict("review_already_resolved")
            if classification.status != "requires_review":
                raise ClassificationReviewConflict("classification_not_reviewable")

            now = self._clock()
            classification.status = "confirmed"
            classification.confirmed_level_1 = decision.level_1.value
            classification.confirmed_level_2 = decision.level_2.value
            classification.confirmed_by = decision.confirmed_by
            classification.confirmed_at = now
            review.status = "resolved"
            review.decision_payload = decision_payload
            review.decided_by = decision.confirmed_by
            review.decided_at = now

            outbox_id = uuid4()
            confirmed_event = ConfirmedClassificationEvent(
                schema_version="album-product-classification-confirmed/1.0",
                event_id=outbox_id,
                event_type=CONFIRMED_TOPIC,
                occurred_at=now,
                opc_id=classification.opc_id,
                album_product_id=classification.album_product_id,
                source_type=classification.source_type,
                classification_id=classification.id,
                review_id=review.id,
                input_event_id=classification.input_event_id,
                input_snapshot_version=classification.input_snapshot_version,
                classification_version=classification.classification_version,
                confirmed_level_1=decision.level_1,
                confirmed_level_2=decision.level_2,
                confirmed_by=decision.confirmed_by,
                confirmed_at=now,
                claims_not_evaluated=(
                    "authenticity",
                    "origin",
                    "quality",
                    "warehouse_acceptance",
                ),
                mall_product_id=None,
                bead_id=None,
            )
            unit_of_work.session.add(
                OutboxEvent(
                    id=outbox_id,
                    topic=CONFIRMED_TOPIC,
                    aggregate_id=classification.id,
                    sequence=1,
                    payload=confirmed_event.model_dump(mode="json"),
                )
            )
            await unit_of_work.session.flush()
            return classification

    async def _ingest_event(
        self,
        session: AsyncSession,
        event: ClassificationCandidateEvent,
    ) -> ClassificationReviewItem:
        existing = await session.scalar(
            select(ProductClassification).where(
                ProductClassification.candidate_event_id == event.event_id
            )
        )
        if existing is not None:
            if not self._same_candidate(existing, event):
                raise ClassificationReviewConflict("candidate_identity_conflict")
            review = await session.scalar(
                select(ClassificationReviewItem).where(
                    ClassificationReviewItem.classification_id == existing.id
                )
            )
            if review is None:
                raise RuntimeError("classification review item is missing")
            return review

        same_version = await session.scalar(
            select(ProductClassification).where(
                ProductClassification.opc_id == event.opc_id,
                ProductClassification.album_product_id == event.album_product_id,
                ProductClassification.input_snapshot_version
                == event.input_snapshot_version,
                ProductClassification.classification_version
                == event.classification_version,
            )
        )
        if same_version is not None:
            raise ClassificationReviewConflict("classification_version_conflict")

        candidates = event.model_dump(mode="json")["candidates"]
        if not isinstance(candidates, list):
            raise TypeError("classification candidates did not serialize as a list")
        classification = ProductClassification(
            opc_id=event.opc_id,
            album_product_id=event.album_product_id,
            source_type=event.source_type.value,
            input_event_id=event.input_event_id,
            candidate_event_id=event.event_id,
            input_snapshot_version=event.input_snapshot_version,
            classification_version=event.classification_version,
            engine_name=event.engine.name,
            engine_version=event.engine.version,
            candidates=candidates,
        )
        session.add(classification)
        await session.flush()
        review = ClassificationReviewItem(
            opc_id=event.opc_id,
            classification_id=classification.id,
        )
        session.add(review)
        await session.flush()
        return review

    @staticmethod
    def _same_candidate(
        existing: ProductClassification,
        event: ClassificationCandidateEvent,
    ) -> bool:
        candidates = event.model_dump(mode="json")["candidates"]
        return (
            existing.opc_id == event.opc_id
            and existing.album_product_id == event.album_product_id
            and existing.source_type == event.source_type.value
            and existing.input_event_id == event.input_event_id
            and existing.input_snapshot_version == event.input_snapshot_version
            and existing.classification_version == event.classification_version
            and existing.engine_name == event.engine.name
            and existing.engine_version == event.engine.version
            and existing.candidates == candidates
        )
