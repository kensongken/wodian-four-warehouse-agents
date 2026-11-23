from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.snapshots import AlbumProductSnapshotService
from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    ConfirmedSnapshotBatch,
)
from photo_organizer.categorization.review import ClassificationReviewService
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem

ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_FIXTURE = ROOT / "contracts/fixtures/first-10-sku-categorization-events.json"
CANDIDATE_FIXTURE = (
    ROOT / "contracts/fixtures/first-10-sku-classification-candidates.json"
)
OPC_ID = "synthetic-opc-001"
NOW = datetime(2026, 8, 17, 10, tzinfo=UTC)


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _authorization_header(
    client: TestClient,
    seeded_invite,
    fake_otp,
) -> tuple[dict[str, str], UUID]:
    phone = "+8613800099201"
    requested = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "product-material-manifest-test",
        },
    )
    assert (requested.status_code, verified.status_code) == (202, 200)
    token = verified.json()["access_token"]
    user_id = UUID(
        str(_app(client).state.token_service.decode_access_token(token)["sub"])
    )
    return {"Authorization": f"Bearer {token}"}, user_id


def _snapshots(count: int) -> ConfirmedSnapshotBatch:
    batch = ConfirmedSnapshotBatch.model_validate_json(
        SNAPSHOT_FIXTURE.read_text(encoding="utf-8")
    )
    return ConfirmedSnapshotBatch(
        schema_version=batch.schema_version,
        batch_id=batch.batch_id,
        generated_at=batch.generated_at,
        data_classification=batch.data_classification,
        events=batch.events[:count],
    )


def _candidates(count: int) -> CategorizationBatchResult:
    batch = CategorizationBatchResult.model_validate_json(
        CANDIDATE_FIXTURE.read_text(encoding="utf-8")
    )
    return CategorizationBatchResult(
        schema_version=batch.schema_version,
        input_batch_id=batch.input_batch_id,
        generated_at=batch.generated_at,
        events=batch.events[:count],
        failures=(),
    )


@pytest_asyncio.fixture(autouse=True)
async def clear_manifest_api_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with auth_session_factory() as session:
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        await session.execute(delete(ClassificationReviewItem))
        await session.execute(delete(ProductClassification))
        await session.execute(delete(AlbumProductSnapshot))
        await session.execute(delete(OpcMembership))
        await session.execute(delete(OpcWorkspace))
        await session.commit()
    yield
    async with auth_session_factory() as session:
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        await session.execute(delete(ClassificationReviewItem))
        await session.execute(delete(ProductClassification))
        await session.execute(delete(AlbumProductSnapshot))
        await session.execute(delete(OpcMembership))
        await session.execute(delete(OpcWorkspace))
        await session.commit()


async def _seed_manifest(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    count: int,
) -> None:
    async with session_factory() as session:
        session.add(OpcWorkspace(id=OPC_ID, display_name="Synthetic OPC"))
        await session.flush()
        session.add(OpcMembership(opc_id=OPC_ID, user_id=user_id, role="reviewer"))
        await session.commit()
    await AlbumProductSnapshotService(session_factory).ingest(_snapshots(count))
    candidates = _candidates(count)
    reviews = await ClassificationReviewService(session_factory).ingest(candidates)
    for index, (candidate, review) in enumerate(zip(candidates.events, reviews)):
        await ClassificationReviewService(
            session_factory,
            clock=lambda index=index: NOW + timedelta(minutes=index),
        ).confirm(
            opc_id=OPC_ID,
            review_id=review.id,
            level_1=candidate.candidates[0].level_1.value,
            level_2=candidate.candidates[0].level_2.value,
            confirmed_by=f"user:{user_id}",
        )


def test_manifest_requires_membership_and_returns_frozen_draft_only_contract(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asyncio.run(_seed_manifest(auth_session_factory, user_id=user_id, count=1))

    response = client.get(
        f"/v1/opcs/{OPC_ID}/product-material-manifest",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "opc-product-material-manifest/1.0"
    assert body["opc_id"] == OPC_ID
    assert body["next_before"] is None
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["album_product_id"] == "album-product-synthetic-001"
    assert item["publication_mode"] == "draft_only"
    assert item["auto_publish"] is False
    assert item["mall_product_id"] is None
    assert item["bead_id"] is None
    assert item["claims_not_evaluated"] == [
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    ]
    assert item["images"][0]["storage_ref"].startswith("evidence/")
    assert "url" not in str(body).lower()


def test_manifest_hides_an_opc_from_nonmembers(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, _ = _authorization_header(client, seeded_invite, fake_otp)

    async def seed_private_workspace() -> None:
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id=OPC_ID, display_name="Private OPC"))
            await session.commit()

    asyncio.run(seed_private_workspace())

    response = client.get(
        f"/v1/opcs/{OPC_ID}/product-material-manifest",
        headers=headers,
    )

    assert (response.status_code, response.json()["detail"]) == (
        404,
        "opc_not_found",
    )


def test_manifest_does_not_cross_opc_paths_and_paginates_without_duplicates(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asyncio.run(_seed_manifest(auth_session_factory, user_id=user_id, count=2))

    async def seed_other_membership() -> None:
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id="opc-other", display_name="Other OPC"))
            await session.flush()
            session.add(
                OpcMembership(opc_id="opc-other", user_id=user_id, role="reviewer")
            )
            await session.commit()

    asyncio.run(seed_other_membership())
    other = client.get(
        "/v1/opcs/opc-other/product-material-manifest",
        headers=headers,
    )
    first = client.get(
        f"/v1/opcs/{OPC_ID}/product-material-manifest",
        params={"limit": 1},
        headers=headers,
    )
    second = client.get(
        f"/v1/opcs/{OPC_ID}/product-material-manifest",
        params={"limit": 1, "before": first.json()["next_before"]},
        headers=headers,
    )

    assert other.status_code == 200
    assert other.json()["items"] == []
    assert first.status_code == second.status_code == 200
    assert first.json()["next_before"] is not None
    first_ids = {item["confirmation_event_id"] for item in first.json()["items"]}
    second_ids = {item["confirmation_event_id"] for item in second.json()["items"]}
    assert len(first_ids) == len(second_ids) == 1
    assert first_ids.isdisjoint(second_ids)


def test_manifest_rejects_invalid_cursor_and_has_no_write_operation(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asyncio.run(_seed_manifest(auth_session_factory, user_id=user_id, count=1))

    invalid = client.get(
        f"/v1/opcs/{OPC_ID}/product-material-manifest",
        params={"before": "not-a-cursor"},
        headers=headers,
    )
    path = _app(client).openapi()["paths"][
        "/v1/opcs/{opc_id}/product-material-manifest"
    ]

    assert (invalid.status_code, invalid.json()["detail"]) == (
        422,
        "invalid_cursor",
    )
    assert set(path) == {"get"}


def test_manifest_enforces_public_page_size_bounds(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asyncio.run(_seed_manifest(auth_session_factory, user_id=user_id, count=1))
    path = f"/v1/opcs/{OPC_ID}/product-material-manifest"

    accepted = [
        client.get(path, params={"limit": limit}, headers=headers) for limit in (1, 50)
    ]
    rejected = [
        client.get(path, params={"limit": limit}, headers=headers) for limit in (0, 51)
    ]

    assert [response.status_code for response in accepted] == [200, 200]
    assert [response.status_code for response in rejected] == [422, 422]
