from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter, ValidationError

from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    CategorizationFailure,
    CategorizationRequest,
    ClassificationCandidateEvent,
    ConfirmedSnapshot,
    ConfirmedSnapshotBatch,
    EngineCandidate,
    EngineIdentity,
    RankedCandidate,
)


class CategorizationUnavailable(Exception):
    """The configured categorization engine is temporarily unavailable."""


class CategorizationEngine(Protocol):
    engine_name: str
    engine_version: str

    async def classify(
        self, request: CategorizationRequest
    ) -> tuple[EngineCandidate, ...]: ...


class CategorizationConsumer:
    _candidate_adapter = TypeAdapter(tuple[EngineCandidate, ...])
    _claims_not_evaluated: tuple[
        Literal["authenticity"],
        Literal["origin"],
        Literal["quality"],
        Literal["warehouse_acceptance"],
    ] = (
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    )

    def __init__(
        self,
        engine: CategorizationEngine,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._engine_identity = EngineIdentity(
            name=engine.engine_name,
            version=engine.engine_version,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    async def consume(self, batch: ConfirmedSnapshotBatch) -> CategorizationBatchResult:
        events: list[ClassificationCandidateEvent] = []
        failures: list[CategorizationFailure] = []
        for source in batch.events:
            request = self._request(source)
            try:
                raw_candidates = await self._engine.classify(request)
            except CategorizationUnavailable:
                failures.append(
                    self._failure(
                        source,
                        code="categorization_unavailable",
                        retryable=True,
                    )
                )
                continue

            try:
                candidates = self._candidate_adapter.validate_python(raw_candidates)
            except ValidationError:
                failures.append(
                    self._failure(
                        source,
                        code="invalid_engine_result",
                        retryable=False,
                    )
                )
                continue
            if not 1 <= len(candidates) <= 3:
                failures.append(
                    self._failure(
                        source,
                        code="invalid_engine_result",
                        retryable=False,
                    )
                )
                continue
            events.append(self._event(source, candidates))

        return CategorizationBatchResult(
            schema_version="album-product-classification-batch/1.0",
            input_batch_id=batch.batch_id,
            generated_at=self._clock(),
            events=tuple(events),
            failures=tuple(failures),
        )

    def _request(self, source: ConfirmedSnapshot) -> CategorizationRequest:
        return CategorizationRequest(
            input_event_id=source.event_id,
            opc_id=source.opc_id,
            album_product_id=source.album_product_id,
            input_snapshot_version=source.snapshot_version,
            source_type=source.source_type,
            images=source.images,
            seed=source.categorization_input,
        )

    def _event(
        self,
        source: ConfirmedSnapshot,
        candidates: tuple[EngineCandidate, ...],
    ) -> ClassificationCandidateEvent:
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                -candidate.confidence,
                candidate.level_1.value,
                candidate.level_2.value,
            ),
        )
        ranked = tuple(
            RankedCandidate(
                rank=rank,
                level_1=candidate.level_1,
                level_2=candidate.level_2,
                confidence=candidate.confidence,
                reason_codes=candidate.reason_codes,
            )
            for rank, candidate in enumerate(ordered, start=1)
        )
        return ClassificationCandidateEvent(
            schema_version="album-product-classification/1.0",
            event_id=uuid5(
                NAMESPACE_URL,
                ":".join(
                    (
                        "photo-organizer",
                        "classification",
                        str(source.event_id),
                        self._engine_identity.name,
                        self._engine_identity.version,
                    )
                ),
            ),
            event_type="album.product.classification.candidate.v1",
            occurred_at=self._clock(),
            input_event_id=source.event_id,
            opc_id=source.opc_id,
            album_product_id=source.album_product_id,
            input_snapshot_version=source.snapshot_version,
            source_type=source.source_type,
            classification_version=1,
            engine=self._engine_identity,
            candidates=ranked,
            review_status="requires_review",
            claims_not_evaluated=self._claims_not_evaluated,
            mall_product_id=None,
            bead_id=None,
        )

    @staticmethod
    def _failure(
        source: ConfirmedSnapshot,
        *,
        code: Literal["categorization_unavailable", "invalid_engine_result"],
        retryable: bool,
    ) -> CategorizationFailure:
        return CategorizationFailure(
            input_event_id=source.event_id,
            album_product_id=source.album_product_id,
            code=code,
            retryable=retryable,
        )
