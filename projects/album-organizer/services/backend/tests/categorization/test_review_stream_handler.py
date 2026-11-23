from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.review_stream import (
    REVIEW_REJECTED_TOPIC,
    ClassificationReviewStreamHandler,
)
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "contracts/fixtures/first-10-sku-classification-candidates.json"
)


def _fields(*, source_event_id: str | None = None) -> dict[str, str]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return {
        "event_id": source_event_id or str(uuid4()),
        "topic": "album.product.classification.candidates.v1",
        "aggregate_id": payload["input_batch_id"],
        "sequence": "1",
        "occurred_at": payload["generated_at"],
        "payload": json.dumps(payload, ensure_ascii=False),
    }


@pytest_asyncio.fixture(autouse=True)
async def clear_review_stream_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async def clear() -> None:
        async with auth_session_factory() as session:
            await session.execute(delete(ClassificationReviewItem))
            await session.execute(delete(ProductClassification))
            await session.execute(
                delete(OutboxEvent).where(OutboxEvent.topic == REVIEW_REJECTED_TOPIC)
            )
            await session.commit()

    await clear()
    yield
    await clear()


@pytest.mark.asyncio
async def test_result_message_commits_ten_reviews_once_before_ack(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handler = ClassificationReviewStreamHandler(auth_session_factory)
    fields = _fields()

    first = await handler.handle("1711000000-0", fields)
    replay = await handler.handle("1711000000-0", fields)

    assert first.acknowledge is True
    assert first.status == "completed"
    assert replay == first
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(ProductClassification.id))) == 10
        assert (
            await session.scalar(select(func.count(ClassificationReviewItem.id))) == 10
        )


@pytest.mark.asyncio
async def test_concurrent_redelivery_creates_one_review_per_candidate(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_handler = ClassificationReviewStreamHandler(auth_session_factory)
    second_handler = ClassificationReviewStreamHandler(auth_session_factory)
    fields = _fields()

    outcomes = await asyncio.gather(
        first_handler.handle("1711000001-0", fields),
        second_handler.handle("1711000001-0", fields),
    )

    assert all(outcome.acknowledge for outcome in outcomes)
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(ProductClassification.id))) == 10
        assert (
            await session.scalar(select(func.count(ClassificationReviewItem.id))) == 10
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("malformed", "invalid_message"),
        ("invalid_event_id", "invalid_message"),
        ("aggregate_mismatch", "aggregate_mismatch"),
    ),
)
async def test_poison_message_is_safely_audited_once(
    auth_session_factory: async_sessionmaker[AsyncSession],
    mutation: str,
    expected_code: str,
) -> None:
    handler = ClassificationReviewStreamHandler(auth_session_factory)
    fields = _fields(source_event_id=str(uuid4()))
    if mutation == "malformed":
        fields["payload"] = json.dumps(
            {"customer_phone": "+886900000000", "signed_url": "secret"}
        )
    elif mutation == "invalid_event_id":
        fields["event_id"] = "not-a-uuid"
    else:
        fields["aggregate_id"] = str(uuid4())

    first = await handler.handle("1711000002-0", fields)
    replay = await handler.handle("1711000002-0", fields)

    assert first.acknowledge is True
    assert first.status == "terminal_failed"
    assert replay == first
    async with auth_session_factory() as session:
        rejected = list(
            (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.topic == REVIEW_REJECTED_TOPIC
                    )
                )
            ).all()
        )
    assert len(rejected) == 1
    assert rejected[0].payload["code"] == expected_code
    serialized = json.dumps(rejected[0].payload)
    assert "customer_phone" not in serialized
    assert "signed_url" not in serialized
    assert "secret" not in serialized


@pytest.mark.asyncio
async def test_version_conflict_is_terminal_without_overwrite(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handler = ClassificationReviewStreamHandler(auth_session_factory)
    await handler.handle("1711000003-0", _fields())
    conflicting = _fields(source_event_id=str(uuid4()))
    payload = json.loads(conflicting["payload"])
    payload["events"][0]["event_id"] = str(uuid4())
    conflicting["payload"] = json.dumps(payload, ensure_ascii=False)

    outcome = await handler.handle("1711000003-1", conflicting)

    assert outcome.acknowledge is True
    assert outcome.status == "terminal_failed"
    async with auth_session_factory() as session:
        rejected = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.topic == REVIEW_REJECTED_TOPIC)
        )
        count = await session.scalar(select(func.count(ProductClassification.id)))
    assert rejected is not None
    assert rejected.payload["code"] == "classification_version_conflict"
    assert count == 10


@pytest.mark.asyncio
async def test_unrelated_topic_is_ignored_without_review_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handler = ClassificationReviewStreamHandler(auth_session_factory)
    fields = _fields()
    fields["topic"] = "ingestion.completed"

    outcome = await handler.handle("1711000004-0", fields)

    assert outcome.acknowledge is True
    assert outcome.status == "ignored"
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(ProductClassification.id))) == 0
