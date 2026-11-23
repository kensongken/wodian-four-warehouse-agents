from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    ConfirmedClassificationEvent,
)
from photo_organizer.categorization.review import (
    ClassificationReviewConflict,
    ClassificationReviewNotFound,
    ClassificationReviewService,
)
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "contracts/fixtures/first-10-sku-classification-candidates.json"
)
NOW = datetime(2026, 8, 16, 14, tzinfo=UTC)


def _batch() -> CategorizationBatchResult:
    return CategorizationBatchResult.model_validate_json(
        FIXTURE_PATH.read_text(encoding="utf-8")
    )


@pytest_asyncio.fixture(autouse=True)
async def clear_review_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with auth_session_factory() as session:
        await session.execute(delete(ClassificationReviewItem))
        await session.execute(delete(ProductClassification))
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        await session.commit()
    yield
    async with auth_session_factory() as session:
        await session.execute(delete(ClassificationReviewItem))
        await session.execute(delete(ProductClassification))
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_first_ten_candidates_create_open_reviews_once(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ClassificationReviewService(auth_session_factory, clock=lambda: NOW)

    first = await service.ingest(_batch())
    replay = await service.ingest(_batch())

    assert len(first) == 10
    assert [item.id for item in replay] == [item.id for item in first]
    async with auth_session_factory() as session:
        classifications = list(
            (await session.scalars(select(ProductClassification))).all()
        )
        reviews = list((await session.scalars(select(ClassificationReviewItem))).all())
    assert len(classifications) == 10
    assert len(reviews) == 10
    assert {classification.status for classification in classifications} == {
        "requires_review"
    }
    assert {review.status for review in reviews} == {"open"}
    assert all(
        classification.confirmed_level_1 is None for classification in classifications
    )
    assert all(
        classification.confirmed_level_2 is None for classification in classifications
    )


@pytest.mark.asyncio
async def test_first_ten_confirmations_emit_versioned_traceable_events(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ClassificationReviewService(auth_session_factory, clock=lambda: NOW)
    batch = _batch()
    reviews = await service.ingest(batch)

    for review, candidate in zip(reviews, batch.events, strict=True):
        selected = candidate.candidates[0]
        await service.confirm(
            opc_id=candidate.opc_id,
            review_id=review.id,
            level_1=selected.level_1.value,
            level_2=selected.level_2.value,
            confirmed_by="operator-synthetic-01",
        )

    async with auth_session_factory() as session:
        persisted_classifications = list(
            (await session.scalars(select(ProductClassification))).all()
        )
        persisted_reviews = list(
            (await session.scalars(select(ClassificationReviewItem))).all()
        )
        outbox_events = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.topic == "album.product.classification.confirmed.v1"
                    )
                )
            ).all()
        )

    confirmations = [
        ConfirmedClassificationEvent.model_validate(event.payload)
        for event in outbox_events
    ]
    assert (
        len(persisted_classifications)
        == len(persisted_reviews)
        == len(confirmations)
        == 10
    )
    assert {review.status for review in persisted_reviews} == {"resolved"}
    classifications_by_candidate = {
        classification.candidate_event_id: classification
        for classification in persisted_classifications
    }
    reviews_by_classification = {
        review.classification_id: review for review in persisted_reviews
    }
    confirmations_by_input = {event.input_event_id: event for event in confirmations}

    for candidate in batch.events:
        classification = classifications_by_candidate[candidate.event_id]
        review = reviews_by_classification[classification.id]
        confirmation = confirmations_by_input[candidate.input_event_id]

        assert confirmation.schema_version == (
            "album-product-classification-confirmed/1.0"
        )
        assert confirmation.opc_id == classification.opc_id == review.opc_id
        assert confirmation.album_product_id == classification.album_product_id
        assert confirmation.source_type.value == classification.source_type
        assert confirmation.classification_id == classification.id
        assert confirmation.review_id == review.id
        assert confirmation.input_snapshot_version == (
            classification.input_snapshot_version
        )
        assert confirmation.classification_version == (
            classification.classification_version
        )
        assert confirmation.claims_not_evaluated == (
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        )
        assert confirmation.mall_product_id is None
        assert confirmation.bead_id is None


@pytest.mark.asyncio
async def test_same_product_snapshot_version_cannot_be_silently_replaced(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ClassificationReviewService(auth_session_factory)
    batch = _batch()
    await service.ingest(batch)
    changed = batch.model_dump(mode="json")
    changed["events"][0]["event_id"] = str(uuid4())
    conflicting = CategorizationBatchResult.model_validate(changed)

    with pytest.raises(
        ClassificationReviewConflict,
        match="classification_version_conflict",
    ):
        await service.ingest(conflicting)

    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(ProductClassification.id))) == 10


@pytest.mark.asyncio
async def test_operator_confirmation_is_tenant_scoped_idempotent_and_audited(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ClassificationReviewService(auth_session_factory, clock=lambda: NOW)
    review = (await service.ingest(_batch()))[0]

    with pytest.raises(ClassificationReviewNotFound, match="review_not_found"):
        await service.confirm(
            opc_id="opc-other",
            review_id=review.id,
            level_1="南红",
            level_2="手串",
            confirmed_by="operator-synthetic-01",
        )

    confirmed = await service.confirm(
        opc_id="synthetic-opc-001",
        review_id=review.id,
        level_1="南红",
        level_2="手串",
        confirmed_by="operator-synthetic-01",
    )
    replay = await service.confirm(
        opc_id="synthetic-opc-001",
        review_id=review.id,
        level_1="南红",
        level_2="手串",
        confirmed_by="operator-synthetic-01",
    )

    assert confirmed.id == replay.id
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_level_1 == "南红"
    assert confirmed.confirmed_level_2 == "手串"
    async with auth_session_factory() as session:
        persisted_review = await session.get(ClassificationReviewItem, review.id)
        outbox = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
    assert persisted_review is not None
    assert persisted_review.status == "resolved"
    assert persisted_review.decision_payload == {
        "level_1": "南红",
        "level_2": "手串",
    }
    assert outbox_count == 1
    assert outbox is not None
    confirmed_event = ConfirmedClassificationEvent.model_validate(outbox.payload)
    assert confirmed_event.event_id == outbox.id
    assert confirmed_event.source_type == "first_sale_bead"
    assert confirmed_event.mall_product_id is None
    assert confirmed_event.bead_id is None
    assert confirmed_event.claims_not_evaluated == (
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    )

    with pytest.raises(
        ClassificationReviewConflict,
        match="review_already_resolved",
    ):
        await service.confirm(
            opc_id="synthetic-opc-001",
            review_id=review.id,
            level_1="其他",
            level_2="手串",
            confirmed_by="operator-synthetic-01",
        )


@pytest.mark.asyncio
async def test_new_snapshot_version_does_not_overwrite_prior_confirmation(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = ClassificationReviewService(auth_session_factory, clock=lambda: NOW)
    batch = _batch()
    first_review = (await service.ingest(batch))[0]
    confirmed = await service.confirm(
        opc_id="synthetic-opc-001",
        review_id=first_review.id,
        level_1="南红",
        level_2="手串",
        confirmed_by="operator-synthetic-01",
    )
    next_data = batch.model_dump(mode="json")
    next_data["input_batch_id"] = str(uuid4())
    next_data["events"] = [next_data["events"][0]]
    next_data["events"][0]["event_id"] = str(uuid4())
    next_data["events"][0]["input_event_id"] = str(uuid4())
    next_data["events"][0]["input_snapshot_version"] = 2
    next_batch = CategorizationBatchResult.model_validate(next_data)

    next_review = (await service.ingest(next_batch))[0]

    assert next_review.id != first_review.id
    async with auth_session_factory() as session:
        old = await session.get(ProductClassification, confirmed.id)
        new = await session.get(ProductClassification, next_review.classification_id)
    assert old is not None
    assert old.status == "confirmed"
    assert old.input_snapshot_version == 1
    assert new is not None
    assert new.status == "requires_review"
    assert new.input_snapshot_version == 2
