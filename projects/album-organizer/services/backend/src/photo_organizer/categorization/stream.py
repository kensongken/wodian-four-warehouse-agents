from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.snapshots import (
    AlbumProductSnapshotConflict,
    AlbumProductSnapshotService,
)
from photo_organizer.categorization.consumer import (
    CategorizationConsumer,
    CategorizationEngine,
)
from photo_organizer.categorization.contracts import ConfirmedSnapshotBatch
from photo_organizer.models.categorization_delivery import CategorizationDelivery
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.repositories.unit_of_work import UnitOfWork

TARGET_TOPIC = "album.product.snapshot.confirmed.v1"
RESULT_TOPIC = "album.product.classification.candidates.v1"
FAILURE_TOPIC = "album.product.classification.failed.v1"
CONSUMER_NAME = "stage2-categorization-v1"


@dataclass(frozen=True)
class DeliveryClaim:
    delivery_id: UUID
    token: UUID
    generation: int
    attempt: int


@dataclass(frozen=True)
class ClaimOutcome:
    status: Literal[
        "acquired",
        "completed",
        "terminal_failed",
        "busy",
        "retry_later",
    ]
    claim: DeliveryClaim | None = None


@dataclass(frozen=True)
class DeliveryOutcome:
    acknowledge: bool
    status: Literal[
        "ignored",
        "completed",
        "terminal_failed",
        "busy",
        "retry_later",
        "retryable_failed",
        "stale",
    ]


class CategorizationDeliveryService:
    retry_delays = (
        timedelta(seconds=2),
        timedelta(seconds=10),
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(minutes=30),
    )
    max_attempts = 8

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_duration = lease_duration

    async def claim(
        self,
        *,
        source_event_id: UUID,
        source_stream_id: str,
        aggregate_id: UUID,
        topic: str,
        payload: dict[str, object],
    ) -> ClaimOutcome:
        now = self._clock()
        async with UnitOfWork(self._session_factory) as unit_of_work:
            await unit_of_work.session.execute(
                insert(CategorizationDelivery)
                .values(
                    consumer_name=CONSUMER_NAME,
                    source_event_id=source_event_id,
                    source_stream_id=source_stream_id,
                    aggregate_id=aggregate_id,
                    topic=topic,
                    input_payload=payload,
                )
                .on_conflict_do_nothing(
                    index_elements=("consumer_name", "source_event_id")
                )
            )
            delivery = await unit_of_work.session.scalar(
                select(CategorizationDelivery)
                .where(
                    CategorizationDelivery.consumer_name == CONSUMER_NAME,
                    CategorizationDelivery.source_event_id == source_event_id,
                )
                .with_for_update()
            )
            if delivery is None:
                raise RuntimeError("categorization delivery insert was not observable")
            if delivery.status == "completed":
                return ClaimOutcome(status="completed")
            if delivery.status == "terminal_failed":
                return ClaimOutcome(status="terminal_failed")
            if (
                delivery.status == "processing"
                and delivery.claim_expires_at is not None
                and delivery.claim_expires_at > now
            ):
                return ClaimOutcome(status="busy")
            if (
                delivery.status == "retryable_failed"
                and delivery.next_attempt_at is not None
                and delivery.next_attempt_at > now
            ):
                return ClaimOutcome(status="retry_later")

            token = uuid4()
            delivery.status = "processing"
            delivery.attempt_count += 1
            delivery.claim_generation += 1
            delivery.claim_token = token
            delivery.claim_expires_at = now + self._lease_duration
            delivery.next_attempt_at = None
            delivery.last_error_code = None
            await unit_of_work.session.flush()
            return ClaimOutcome(
                status="acquired",
                claim=DeliveryClaim(
                    delivery_id=delivery.id,
                    token=token,
                    generation=delivery.claim_generation,
                    attempt=delivery.attempt_count,
                ),
            )

    async def complete(
        self,
        claim: DeliveryClaim,
        *,
        result_payload: dict[str, object],
        aggregate_id: UUID,
    ) -> bool:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            delivery = await self._owned_delivery(unit_of_work.session, claim)
            if delivery is None:
                return False
            delivery.status = "completed"
            delivery.result_payload = result_payload
            delivery.completed_at = self._clock()
            self._release_claim(delivery)
            unit_of_work.session.add(
                OutboxEvent(
                    topic=RESULT_TOPIC,
                    aggregate_id=aggregate_id,
                    sequence=1,
                    payload=result_payload,
                )
            )
            await unit_of_work.session.flush()
            return True

    async def retry_or_exhaust(
        self,
        claim: DeliveryClaim,
        *,
        result_payload: dict[str, object],
    ) -> Literal["retryable_failed", "terminal_failed", "stale"]:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            delivery = await self._owned_delivery(unit_of_work.session, claim)
            if delivery is None:
                return "stale"
            delivery.result_payload = result_payload
            if claim.attempt < self.max_attempts:
                delivery.status = "retryable_failed"
                delivery.last_error_code = "categorization_unavailable"
                delivery.next_attempt_at = (
                    self._clock() + self.retry_delays[claim.attempt - 1]
                )
                self._release_claim(delivery)
                await unit_of_work.session.flush()
                return "retryable_failed"

            delivery.status = "terminal_failed"
            delivery.last_error_code = "categorization_retry_exhausted"
            delivery.completed_at = self._clock()
            self._release_claim(delivery)
            unit_of_work.session.add(self._failure_event(delivery))
            await unit_of_work.session.flush()
            return "terminal_failed"

    async def terminal_failure(
        self,
        claim: DeliveryClaim,
        *,
        code: str,
    ) -> bool:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            delivery = await self._owned_delivery(unit_of_work.session, claim)
            if delivery is None:
                return False
            delivery.status = "terminal_failed"
            delivery.last_error_code = code
            delivery.completed_at = self._clock()
            self._release_claim(delivery)
            unit_of_work.session.add(self._failure_event(delivery))
            await unit_of_work.session.flush()
            return True

    @staticmethod
    async def _owned_delivery(
        session: AsyncSession, claim: DeliveryClaim
    ) -> CategorizationDelivery | None:
        return await session.scalar(
            select(CategorizationDelivery)
            .where(
                CategorizationDelivery.id == claim.delivery_id,
                CategorizationDelivery.status == "processing",
                CategorizationDelivery.claim_token == claim.token,
                CategorizationDelivery.claim_generation == claim.generation,
            )
            .with_for_update()
        )

    @staticmethod
    def _release_claim(delivery: CategorizationDelivery) -> None:
        delivery.claim_token = None
        delivery.claim_expires_at = None

    @staticmethod
    def _failure_event(delivery: CategorizationDelivery) -> OutboxEvent:
        return OutboxEvent(
            topic=FAILURE_TOPIC,
            aggregate_id=delivery.aggregate_id,
            sequence=1,
            payload={
                "schema_version": "categorization-delivery-failure/1.0",
                "source_event_id": str(delivery.source_event_id),
                "code": delivery.last_error_code,
            },
        )


class CategorizationStreamHandler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        engine: CategorizationEngine,
        *,
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        snapshot_service: AlbumProductSnapshotService | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._consumer = CategorizationConsumer(engine, clock=self._clock)
        self._snapshots = snapshot_service or AlbumProductSnapshotService(
            session_factory
        )
        self._deliveries = CategorizationDeliveryService(
            session_factory,
            clock=self._clock,
            lease_duration=lease_duration,
        )

    async def handle(
        self, source_stream_id: str | bytes, raw_fields: Mapping[object, object]
    ) -> DeliveryOutcome:
        stream_id = self._text(source_stream_id)
        fields = {
            self._text(key): self._text(value) for key, value in raw_fields.items()
        }
        if fields.get("topic") != TARGET_TOPIC:
            return DeliveryOutcome(acknowledge=True, status="ignored")

        fallback_id = uuid5(
            NAMESPACE_URL,
            f"photo-organizer:{CONSUMER_NAME}:{stream_id}",
        )
        envelope_valid = True
        try:
            source_event_id = UUID(fields["event_id"])
        except (KeyError, ValueError):
            source_event_id = fallback_id
            envelope_valid = False
        try:
            aggregate_id = UUID(fields["aggregate_id"])
        except (KeyError, ValueError):
            aggregate_id = fallback_id
            envelope_valid = False
        try:
            decoded = json.loads(fields["payload"])
            if not isinstance(decoded, dict):
                raise TypeError("payload must be an object")
            payload: dict[str, object] = decoded
        except (KeyError, ValueError, TypeError):
            payload = {}
            envelope_valid = False

        batch: ConfirmedSnapshotBatch | None = None
        if envelope_valid:
            try:
                batch = ConfirmedSnapshotBatch.model_validate(payload)
            except ValidationError:
                # Invalid target payloads may contain fields outside the closed
                # contract. Persist only a safe empty body plus the delivery
                # identity and error code.
                payload = {}

        claimed = await self._deliveries.claim(
            source_event_id=source_event_id,
            source_stream_id=stream_id,
            aggregate_id=aggregate_id,
            topic=TARGET_TOPIC,
            payload=payload,
        )
        if claimed.status in {"completed", "terminal_failed"}:
            return DeliveryOutcome(acknowledge=True, status=claimed.status)
        if claimed.status in {"busy", "retry_later"}:
            return DeliveryOutcome(acknowledge=False, status=claimed.status)
        if claimed.claim is None:
            raise RuntimeError("acquired delivery did not return a claim")

        if not envelope_valid or batch is None:
            finalized = await self._deliveries.terminal_failure(
                claimed.claim,
                code="invalid_message",
            )
            return DeliveryOutcome(
                acknowledge=finalized,
                status="terminal_failed" if finalized else "stale",
            )

        try:
            await self._snapshots.ingest(batch)
        except AlbumProductSnapshotConflict:
            finalized = await self._deliveries.terminal_failure(
                claimed.claim,
                code="snapshot_identity_conflict",
            )
            return DeliveryOutcome(
                acknowledge=finalized,
                status="terminal_failed" if finalized else "stale",
            )

        result = await self._consumer.consume(batch)
        result_payload = result.model_dump(mode="json")
        if any(failure.retryable for failure in result.failures):
            status = await self._deliveries.retry_or_exhaust(
                claimed.claim,
                result_payload=result_payload,
            )
            return DeliveryOutcome(
                acknowledge=status == "terminal_failed",
                status=status,
            )

        completed = await self._deliveries.complete(
            claimed.claim,
            result_payload=result_payload,
            aggregate_id=batch.batch_id,
        )
        return DeliveryOutcome(
            acknowledge=completed,
            status="completed" if completed else "stale",
        )

    @staticmethod
    def _text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)


class RedisConsumerStream(Protocol):
    async def xgroup_create(
        self, name: str, groupname: str, id: str, mkstream: bool
    ) -> object: ...

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        count: int,
    ) -> Any: ...

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int,
        block: int,
    ) -> Any: ...

    async def xack(
        self, name: str, groupname: str, stream_id: str | bytes
    ) -> object: ...


class StreamDeliveryHandler(Protocol):
    async def handle(
        self, source_stream_id: str | bytes, raw_fields: Mapping[object, object]
    ) -> DeliveryOutcome: ...


class CategorizationRedisWorker:
    stream_name = "photo-organizer-events"
    group_name = CONSUMER_NAME

    def __init__(
        self,
        redis: RedisConsumerStream,
        handler: StreamDeliveryHandler,
        *,
        consumer_name: str | None = None,
        reclaim_idle_ms: int = 1_000,
    ) -> None:
        self._redis = redis
        self._handler = handler
        self._consumer_name = consumer_name or f"worker-{uuid4()}"
        self._reclaim_idle_ms = reclaim_idle_ms
        self._group_ready = False

    async def run_once(self, *, block_ms: int = 1_000, count: int = 20) -> int:
        if not 1 <= count <= 100:
            raise ValueError("count must be between 1 and 100")
        if block_ms < 0:
            raise ValueError("block_ms must be nonnegative")
        await self._ensure_group()
        entries = await self._reclaimed(count)
        if not entries:
            entries = await self._fresh(block_ms=block_ms, count=count)

        for stream_id, fields in entries:
            outcome = await self._handler.handle(stream_id, fields)
            if outcome.acknowledge:
                await self._redis.xack(
                    self.stream_name,
                    self.group_name,
                    stream_id,
                )
        return len(entries)

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._redis.xgroup_create(
                self.stream_name,
                self.group_name,
                "0",
                True,
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
        self._group_ready = True

    async def _reclaimed(
        self, count: int
    ) -> list[tuple[str | bytes, Mapping[object, object]]]:
        response = await self._redis.xautoclaim(
            self.stream_name,
            self.group_name,
            self._consumer_name,
            self._reclaim_idle_ms,
            "0-0",
            count,
        )
        if not isinstance(response, (list, tuple)) or len(response) < 2:
            return []
        return self._coerce_entries(response[1])

    async def _fresh(
        self, *, block_ms: int, count: int
    ) -> list[tuple[str | bytes, Mapping[object, object]]]:
        response = await self._redis.xreadgroup(
            self.group_name,
            self._consumer_name,
            {self.stream_name: ">"},
            count,
            block_ms,
        )
        if not isinstance(response, (list, tuple)):
            return []
        entries: list[tuple[str | bytes, Mapping[object, object]]] = []
        for stream_response in response:
            if (
                not isinstance(stream_response, (list, tuple))
                or len(stream_response) < 2
            ):
                continue
            entries.extend(self._coerce_entries(stream_response[1]))
        return entries

    @staticmethod
    def _coerce_entries(
        value: object,
    ) -> list[tuple[str | bytes, Mapping[object, object]]]:
        if not isinstance(value, (list, tuple)):
            return []
        entries: list[tuple[str | bytes, Mapping[object, object]]] = []
        for entry in value:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            stream_id, fields = entry
            if not isinstance(stream_id, (str, bytes)) or not isinstance(
                fields, Mapping
            ):
                continue
            entries.append((stream_id, fields))
        return entries
