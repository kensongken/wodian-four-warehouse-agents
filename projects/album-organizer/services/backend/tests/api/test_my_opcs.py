import asyncio
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.user import User


async def _clear_opc_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(delete(OpcMembership))
        await session.execute(delete(OpcWorkspace))
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def clear_opc_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await _clear_opc_state(auth_session_factory)
    yield
    await _clear_opc_state(auth_session_factory)


def _authorization_header(
    client: TestClient,
    seeded_invite,
    fake_otp,
    *,
    phone: str = "+8613800099201",
) -> tuple[dict[str, str], UUID]:
    requested = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "my-opcs-test-device",
        },
    )
    assert (requested.status_code, verified.status_code) == (202, 200)
    token = verified.json()["access_token"]
    app = cast(FastAPI, client.app)
    user_id = UUID(str(app.state.token_service.decode_access_token(token)["sub"]))
    return {"Authorization": f"Bearer {token}"}, user_id


def test_my_opcs_requires_a_bearer_token(client: TestClient) -> None:
    """Removing authentication must expose OPC membership metadata anonymously."""
    response = client.get("/v1/me/opcs")

    assert (response.status_code, response.json()["detail"]) == (
        401,
        "invalid_bearer_token",
    )


def test_my_opcs_returns_an_empty_list_for_a_user_without_memberships(
    client: TestClient,
    seeded_invite,
    fake_otp,
) -> None:
    """Treating a missing membership as an error must block a valid empty account."""
    headers, _ = _authorization_header(client, seeded_invite, fake_otp)

    response = client.get("/v1/me/opcs", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_my_opcs_returns_only_current_memberships_in_stable_order(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dropping the user filter or stable order must mix or reorder OPC authority."""
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)

    async def seed_memberships() -> None:
        async with auth_session_factory() as session:
            other_user = User(phone_e164="+8613800099202")
            session.add_all(
                [
                    other_user,
                    OpcWorkspace(id="opc-z", display_name="Z OPC"),
                    OpcWorkspace(id="opc-a", display_name="A OPC"),
                    OpcWorkspace(id="opc-private", display_name="Private OPC"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    OpcMembership(opc_id="opc-z", user_id=user_id, role="owner"),
                    OpcMembership(opc_id="opc-a", user_id=user_id, role="reviewer"),
                    OpcMembership(
                        opc_id="opc-private",
                        user_id=other_user.id,
                        role="owner",
                    ),
                ]
            )
            await session.commit()

    asyncio.run(seed_memberships())
    response = client.get("/v1/me/opcs", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"id": "opc-a", "display_name": "A OPC", "role": "reviewer"},
            {"id": "opc-z", "display_name": "Z OPC", "role": "owner"},
        ]
    }
