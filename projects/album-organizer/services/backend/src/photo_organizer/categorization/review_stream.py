from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.contracts import CategorizationBatchResult
from photo_organizer.categorization.review import (
    ClassificationReviewConflict,
    ClassificationReviewService,
)
from photo_organizer.categorization.stream import (
    CategorizationRedisWorker,
    DeliveryOutcome,
    RedisConsumerStream,
    StreamDeliveryHandler,
)
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.repositories.unit_of_work import UnitOfWork

REVIEW_TARGET_TOPIC: Literal["album.product.classification.candidates.v1"] = (
    "album.product.classification.candidates.v1"
)
REVIEW_REJECTED_TOPIC: Literal["album.product.classification.review-rejected.v1"] = (
    "album.product.classification.review-rejected.v1"
)
REVIEW_GROUP = "stage2-classification-review-v1"
SAFE_CONFLICT_CODES = frozenset(
    {
        "candidate_identity_conflict",
        "classification_version_conflict",
    }
)


class ClassificationReviewStreamHandler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reviews = ClassificationReviewService(
            session_factory,
            clock=self._clock,
        )

    async def handle(
        self, source_stream_id: str | bytes, raw_fields: Mapping[object, object]
    ) -> DeliveryOutcome:
        stream_id = self._text(source_stream_id)
        fields = {
            self._text(key): self._text(value) for key, value in raw_fields.items()
        }
        if fields.get("topic") != REVIEW_TARGET_TOPIC:
            return DeliveryOutcome(acknowledge=True, status="ignored")

        fallback_id = uuid5(
            NAMESPACE_URL,
            f"photo-organizer:{REVIEW_GROUP}:{stream_id}",
        )
        source_identity_valid = True
        try:
            source_event_id = UUID(fields["event_id"])
        except (KeyError, ValueError):
            source_event_id = fallback_id
            source_identity_valid = False

        try:
            aggregate_id = UUID(fields["aggregate_id"])
            decoded = json.loads(fields["payload"])
            if not isinstance(decoded, dict):
                raise TypeError("payload must be an object")
            batch = CategorizationBatchResult.model_validate(decoded)
        except (KeyError, TypeError, ValueError, ValidationError):
            await self._reject(source_event_id, code="invalid_message")
            return DeliveryOutcome(acknowledge=True, status="terminal_failed")

        if not source_identity_valid:
            await self._reject(source_event_id, code="invalid_message")
            return DeliveryOutcome(acknowledge=True, status="terminal_failed")

        if aggregate_id != batch.input_batch_id:
            await self._reject(source_event_id, code="aggregate_mismatch")
            return DeliveryOutcome(acknowledge=True, status="terminal_failed")

        try:
            await self._reviews.ingest(batch)
        except ClassificationReviewConflict as error:
            code = str(error)
            await self._reject(
                source_event_id,
                code=code if code in SAFE_CONFLICT_CODES else "classification_conflict",
            )
            return DeliveryOutcome(acknowledge=True, status="terminal_failed")
        return DeliveryOutcome(acknowledge=True, status="completed")

    async def _reject(self, source_event_id: UUID, *, code: str) -> None:
        event_id = uuid5(
            NAMESPACE_URL,
            f"photo-organizer:classification-review-rejected:{source_event_id}",
        )
        async with UnitOfWork(self._session_factory) as unit_of_work:
            await unit_of_work.session.execute(
                insert(OutboxEvent)
                .values(
                    id=event_id,
                    topic=REVIEW_REJECTED_TOPIC,
                    aggregate_id=source_event_id,
                    sequence=1,
                    payload={
                        "schema_version": "classification-review-rejected/1.0",
                        "event_id": str(event_id),
                        "event_type": REVIEW_REJECTED_TOPIC,
                        "occurred_at": self._clock().isoformat(),
                        "source_event_id": str(source_event_id),
                        "code": code,
                    },
                )
                .on_conflict_do_nothing()
            )

    @staticmethod
    def _text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)


class ClassificationReviewRedisWorker(CategorizationRedisWorker):
    group_name = REVIEW_GROUP

    def __init__(
        self,
        redis: RedisConsumerStream,
        handler: StreamDeliveryHandler,
        *,
        consumer_name: str | None = None,
        reclaim_idle_ms: int = 1_000,
    ) -> None:
        super().__init__(
            redis,
            handler,
            consumer_name=consumer_name,
            reclaim_idle_ms=reclaim_idle_ms,
        )
