from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from photo_organizer.categorization.consumer import (
    CategorizationConsumer,
    CategorizationUnavailable,
)
from photo_organizer.categorization.contracts import (
    CategorizationRequest,
    ConfirmedSnapshotBatch,
    EngineCandidate,
    ReasonCode,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "contracts/fixtures/first-10-sku-categorization-events.json"
)
NOW = datetime(2026, 8, 16, 10, 30, tzinfo=UTC)


def _batch() -> ConfirmedSnapshotBatch:
    return ConfirmedSnapshotBatch.model_validate_json(
        FIXTURE_PATH.read_text(encoding="utf-8")
    )


class DeterministicEngine:
    engine_name = "fixture-categorizer"
    engine_version = "1.0"

    def __init__(
        self,
        *,
        unavailable_product: str | None = None,
        invalid_product: str | None = None,
        unexpected_product: str | None = None,
    ) -> None:
        self.unavailable_product = unavailable_product
        self.invalid_product = invalid_product
        self.unexpected_product = unexpected_product
        self.requests: list[CategorizationRequest] = []

    async def classify(
        self, request: CategorizationRequest
    ) -> tuple[EngineCandidate, ...]:
        self.requests.append(request)
        if request.album_product_id == self.unavailable_product:
            raise CategorizationUnavailable
        if request.album_product_id == self.unexpected_product:
            raise RuntimeError("programming defect must remain visible")
        if request.album_product_id == self.invalid_product:
            return ()
        return (
            EngineCandidate(
                level_1=request.seed.level_1_candidate,
                level_2=request.seed.level_2_candidate,
                confidence=0.91,
                reason_codes=(ReasonCode.SEED_MATCH,),
            ),
        )


@pytest.mark.asyncio
async def test_first_ten_skus_produce_versioned_review_candidates() -> None:
    engine = DeterministicEngine()
    consumer = CategorizationConsumer(engine, clock=lambda: NOW)

    result = await consumer.consume(_batch())

    assert len(result.events) == 10
    assert result.failures == ()
    assert len(engine.requests) == 10
    for source, event in zip(_batch().events, result.events, strict=True):
        assert event.input_event_id == source.event_id
        assert event.opc_id == source.opc_id
        assert event.album_product_id == source.album_product_id
        assert event.input_snapshot_version == source.snapshot_version
        assert event.source_type == source.source_type
        assert event.review_status == "requires_review"
        assert event.mall_product_id is None
        assert event.bead_id is None
        assert (
            event.candidates[0].level_1 == source.categorization_input.level_1_candidate
        )
        assert (
            event.candidates[0].level_2 == source.categorization_input.level_2_candidate
        )
        assert event.claims_not_evaluated == (
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        )


@pytest.mark.asyncio
async def test_same_input_and_engine_version_replay_the_same_event_ids() -> None:
    consumer = CategorizationConsumer(DeterministicEngine(), clock=lambda: NOW)

    first = await consumer.consume(_batch())
    second = await consumer.consume(_batch())

    assert [event.event_id for event in first.events] == [
        event.event_id for event in second.events
    ]


@pytest.mark.asyncio
async def test_one_unavailable_sku_does_not_block_the_other_nine() -> None:
    engine = DeterministicEngine(unavailable_product="album-product-synthetic-005")

    result = await CategorizationConsumer(engine, clock=lambda: NOW).consume(_batch())

    assert len(result.events) == 9
    assert len(result.failures) == 1
    assert result.failures[0].album_product_id == "album-product-synthetic-005"
    assert result.failures[0].code == "categorization_unavailable"
    assert result.failures[0].retryable is True
    assert "storage_ref" not in json.dumps(result.failures[0].model_dump(mode="json"))


@pytest.mark.asyncio
async def test_invalid_engine_result_is_nonretryable_and_isolated() -> None:
    engine = DeterministicEngine(invalid_product="album-product-synthetic-006")

    result = await CategorizationConsumer(engine, clock=lambda: NOW).consume(_batch())

    assert len(result.events) == 9
    assert len(result.failures) == 1
    assert result.failures[0].album_product_id == "album-product-synthetic-006"
    assert result.failures[0].code == "invalid_engine_result"
    assert result.failures[0].retryable is False


@pytest.mark.asyncio
async def test_unexpected_engine_error_is_not_disguised_as_business_failure() -> None:
    engine = DeterministicEngine(unexpected_product="album-product-synthetic-007")

    with pytest.raises(RuntimeError, match="programming defect"):
        await CategorizationConsumer(engine, clock=lambda: NOW).consume(_batch())
