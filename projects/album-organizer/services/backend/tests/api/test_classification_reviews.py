import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.contracts import CategorizationBatchResult
from photo_organizer.categorization.review import ClassificationReviewService
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem
from photo_organizer.models.user import User

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "contracts/fixtures/first-10-sku-classification-candidates.json"
)
OPC_ID = "synthetic-opc-001"


def _batch() -> CategorizationBatchResult:
    return CategorizationBatchResult.model_validate_json(
        FIXTURE_PATH.read_text(encoding="utf-8")
    )


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _authorization_header(
    client: TestClient, seeded_invite, fake_otp
) -> tuple[dict[str, str], UUID]:
    phone = "+8613800099101"
    requested = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "classification-review-test",
        },
    )
    assert (requested.status_code, verified.status_code) == (202, 200)
    token = verified.json()["access_token"]
    user_id = UUID(
        str(_app(client).state.token_service.decode_access_token(token)["sub"])
    )
    return {"Authorization": f"Bearer {token}"}, user_id


async def _clear_review_api_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(delete(ClassificationReviewItem))
        await session.execute(delete(ProductClassification))
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.topic == "album.product.classification.confirmed.v1"
            )
        )
        await session.execute(delete(OpcMembership))
        await session.execute(delete(OpcWorkspace))
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def clear_review_api_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await _clear_review_api_state(auth_session_factory)
    yield
    await _clear_review_api_state(auth_session_factory)


async def _seed_member_and_reviews(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    *,
    role: str = "reviewer",
) -> tuple[ClassificationReviewItem, ...]:
    async with session_factory() as session:
        session.add(OpcWorkspace(id=OPC_ID, display_name="Synthetic OPC"))
        await session.flush()
        session.add(OpcMembership(opc_id=OPC_ID, user_id=user_id, role=role))
        await session.commit()
    return await ClassificationReviewService(session_factory).ingest(_batch())


@pytest.mark.asyncio
async def test_opc_membership_persistence_rejects_duplicates_and_unknown_roles(
    db_session: AsyncSession,
) -> None:
    """Removing either membership invariant must let an ambiguous authority persist."""
    user = User(phone_e164="+8613800099001")
    workspace = OpcWorkspace(id="synthetic-opc-001", display_name="Synthetic OPC")
    db_session.add_all([user, workspace])
    await db_session.flush()
    db_session.add(OpcMembership(opc_id=workspace.id, user_id=user.id, role="reviewer"))
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                OpcMembership(opc_id=workspace.id, user_id=user.id, role="owner")
            )
            await db_session.flush()

    second_user = User(phone_e164="+8613800099002")
    db_session.add(second_user)
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                OpcMembership(
                    opc_id=workspace.id,
                    user_id=second_user.id,
                    role="viewer",
                )
            )
            await db_session.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("opc_id", ["ab", "-invalid", "invalid/opc"])
async def test_opc_workspace_rejects_ids_outside_the_event_identity_contract(
    db_session: AsyncSession,
    opc_id: str,
) -> None:
    """Permitting an unaddressable OPC ID must create authority no API can name."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(OpcWorkspace(id=opc_id, display_name="Invalid OPC"))
            await db_session.flush()


@pytest.mark.asyncio
async def test_opc_workspace_rejects_a_blank_display_name(
    db_session: AsyncSession,
) -> None:
    """A blank provisioned workspace name must bypass the administrative contract."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(OpcWorkspace(id="opc-blank-name", display_name=""))
            await db_session.flush()


def test_review_list_requires_bearer_and_hides_an_opc_from_non_members(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Trusting the path OPC without membership must expose another tenant's queue."""
    missing_bearer = client.get(f"/v1/opcs/{OPC_ID}/classification-reviews")
    headers, _ = _authorization_header(client, seeded_invite, fake_otp)

    async def seed_workspace_without_membership() -> None:
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id=OPC_ID, display_name="Private OPC"))
            await session.commit()

    asyncio.run(seed_workspace_without_membership())
    non_member = client.get(
        f"/v1/opcs/{OPC_ID}/classification-reviews", headers=headers
    )

    assert (missing_bearer.status_code, missing_bearer.json()["detail"]) == (
        401,
        "invalid_bearer_token",
    )
    assert (non_member.status_code, non_member.json()["detail"]) == (
        404,
        "opc_not_found",
    )


def test_member_lists_only_open_reviews_with_stable_cursor_and_safe_fields(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dropping status, tenant, or UUID tie-break filters must corrupt the review queue."""
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    reviews = asyncio.run(_seed_member_and_reviews(auth_session_factory, user_id))

    first_page = client.get(
        f"/v1/opcs/{OPC_ID}/classification-reviews",
        params={"limit": 3},
        headers=headers,
    )
    expected_ids = sorted((str(review.id) for review in reviews), reverse=True)

    assert first_page.status_code == 200
    assert [item["id"] for item in first_page.json()["items"]] == expected_ids[:3]
    assert first_page.json()["next_before"] is not None
    first_item = first_page.json()["items"][0]
    assert first_item["status"] == "open"
    assert first_item["source_type"] in {
        "first_sale_bead",
        "legacy_material",
        "redesign_output",
    }
    assert first_item["claims_not_evaluated"] == [
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    ]
    assert "images" not in first_item
    assert "storage_ref" not in str(first_item)
    assert first_item["mall_product_id"] is None
    assert first_item["bead_id"] is None

    second_page = client.get(
        f"/v1/opcs/{OPC_ID}/classification-reviews",
        params={"limit": 3, "before": first_page.json()["next_before"]},
        headers=headers,
    )
    assert second_page.status_code == 200
    assert [item["id"] for item in second_page.json()["items"]] == expected_ids[3:6]
    assert not (
        {item["id"] for item in first_page.json()["items"]}
        & {item["id"] for item in second_page.json()["items"]}
    )


def test_member_cannot_read_a_review_through_another_opc_path(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Looking up a review by UUID alone must leak its classification across OPCs."""
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    review = asyncio.run(_seed_member_and_reviews(auth_session_factory, user_id))[0]

    async def seed_second_membership() -> None:
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id="opc-other", display_name="Other OPC"))
            await session.flush()
            session.add(
                OpcMembership(opc_id="opc-other", user_id=user_id, role="reviewer")
            )
            await session.commit()

    asyncio.run(seed_second_membership())

    wrong_opc = client.get(
        f"/v1/opcs/opc-other/classification-reviews/{review.id}", headers=headers
    )
    missing_review = client.get(
        f"/v1/opcs/{OPC_ID}/classification-reviews/{UUID(int=0)}", headers=headers
    )

    assert (wrong_opc.status_code, wrong_opc.json()["detail"]) == (
        404,
        "review_not_found",
    )
    assert (missing_review.status_code, missing_review.json()["detail"]) == (
        404,
        "review_not_found",
    )


def test_confirmation_uses_authenticated_actor_is_idempotent_and_filters_status(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Accepting a body actor or non-atomic replay must forge audit history."""
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    review = asyncio.run(_seed_member_and_reviews(auth_session_factory, user_id))[0]
    path = f"/v1/opcs/{OPC_ID}/classification-reviews/{review.id}/confirm"
    body = {"level_1": "南红", "level_2": "手串"}

    forged = client.post(path, json={**body, "confirmed_by": "forged"}, headers=headers)
    confirmed = client.post(path, json=body, headers=headers)
    replay = client.post(path, json=body, headers=headers)
    open_items = client.get(
        f"/v1/opcs/{OPC_ID}/classification-reviews",
        headers=headers,
    )
    resolved_items = client.get(
        f"/v1/opcs/{OPC_ID}/classification-reviews",
        params={"status": "resolved"},
        headers=headers,
    )

    assert forged.status_code == 422
    assert (confirmed.status_code, replay.status_code) == (200, 200)
    assert confirmed.json() == replay.json()
    assert confirmed.json()["confirmed_by"] == f"user:{user_id}"
    assert confirmed.json()["source_type"] == "first_sale_bead"
    assert len(open_items.json()["items"]) == 9
    assert [item["id"] for item in resolved_items.json()["items"]] == [str(review.id)]

    async def persisted_audit() -> tuple[str | None, int]:
        async with auth_session_factory() as session:
            classification = await session.get(
                ProductClassification, review.classification_id
            )
            count = await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic == "album.product.classification.confirmed.v1"
                )
            )
        assert classification is not None
        return classification.confirmed_by, int(count or 0)

    assert asyncio.run(persisted_audit()) == (f"user:{user_id}", 1)


def test_public_review_schema_closes_candidate_and_version_bounds(
    client: TestClient,
) -> None:
    """Weakening response bounds must let malformed persisted candidates leak outward."""
    schema = _app(client).openapi()["components"]["schemas"][
        "ClassificationReviewResponse"
    ]["properties"]

    assert schema["candidates"]["minItems"] == 1
    assert schema["candidates"]["maxItems"] == 3
    assert schema["input_snapshot_version"]["minimum"] == 1
    assert schema["classification_version"]["minimum"] == 1
    assert schema["opc_id"]["minLength"] == 3
    assert schema["opc_id"]["maxLength"] == 128
