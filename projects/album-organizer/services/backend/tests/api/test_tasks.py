import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User


def _authorization_header(
    client: TestClient, seeded_invite, fake_otp
) -> dict[str, str]:
    phone = "+8613800011001"
    requested = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "task-test",
        },
    )
    assert (requested.status_code, verified.status_code) == (202, 200)
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def test_create_task_trims_name_and_returns_summary(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Removing request normalization or tenant task creation breaks this contract."""
    response = client.post(
        "/v1/tasks",
        json={"name": "  Autumn launch  "},
        headers=_authorization_header(client, seeded_invite, fake_otp),
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Autumn launch"
    assert response.json()["status"] == "waiting_upload"
    assert response.json()["total_assets"] == 0


def test_task_api_hides_another_users_task_as_not_found(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Looking up a task by ID without the owner scope must leak tenant data."""
    headers = _authorization_header(client, seeded_invite, fake_otp)
    created = client.post("/v1/tasks", json={"name": "Private"}, headers=headers).json()
    first_user_id = UUID(
        str(
            _app(client).state.token_service.decode_access_token(
                headers["Authorization"][7:]
            )["sub"]
        )
    )

    async def other_user_token() -> str:
        second_user = User(phone_e164="+8613800011002")
        async with auth_session_factory() as session:
            session.add(second_user)
            await session.commit()
        return (
            await _app(client).state.token_service.issue_pair(
                second_user.id, "other-device"
            )
        ).access_token

    other_headers = {"Authorization": f"Bearer {asyncio.run(other_user_token())}"}
    response = client.get(f"/v1/tasks/{created['id']}", headers=other_headers)

    assert first_user_id != UUID(
        str(
            _app(client).state.token_service.decode_access_token(
                other_headers["Authorization"][7:]
            )["sub"]
        )
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "task_not_found"


def test_list_tasks_uses_newest_first_bounded_cursor_pagination(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ignoring the UUID tie-breaker can lose tasks when timestamps are equal."""
    headers = _authorization_header(client, seeded_invite, fake_otp)
    created = [
        client.post(
            "/v1/tasks", json={"name": f"Task {number}"}, headers=headers
        ).json()
        for number in range(3)
    ]

    async def make_creation_timestamps_identical() -> None:
        async with auth_session_factory() as session:
            await session.execute(
                update(ImportTask)
                .where(ImportTask.id.in_([UUID(item["id"]) for item in created]))
                .values(created_at=datetime(2026, 8, 14, tzinfo=UTC))
            )
            await session.commit()

    asyncio.run(make_creation_timestamps_identical())
    first_page = client.get("/v1/tasks?limit=2", headers=headers)
    expected_ids = sorted((item["id"] for item in created), reverse=True)

    assert first_page.status_code == 200
    assert [item["id"] for item in first_page.json()["items"]] == expected_ids[:2]
    second_page = client.get(
        f"/v1/tasks?limit=2&before={first_page.json()['next_before']}", headers=headers
    )
    assert second_page.status_code == 200
    assert [item["id"] for item in second_page.json()["items"]] == expected_ids[2:]
    assert second_page.json()["next_before"] is None


def test_task_routes_reject_blank_or_oversized_names_and_invalid_page_size(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Letting invalid task input reach persistence bypasses the API boundary."""
    headers = _authorization_header(client, seeded_invite, fake_otp)
    blank = client.post("/v1/tasks", json={"name": "   "}, headers=headers)
    oversized = client.post("/v1/tasks", json={"name": "x" * 81}, headers=headers)
    unbounded = client.get("/v1/tasks?limit=51", headers=headers)

    assert (blank.status_code, oversized.status_code, unbounded.status_code) == (
        422,
        422,
        422,
    )


def test_task_creation_rejects_non_string_names_and_exposes_name_bounds(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Non-string task names must return 422 and the public schema must show bounds."""
    headers = _authorization_header(client, seeded_invite, fake_otp)
    responses = [
        client.post("/v1/tasks", json={"name": value}, headers=headers)
        for value in (123, None, ["not", "a", "name"])
    ]
    schema = _app(client).openapi()["components"]["schemas"]["CreateTaskRequest"]

    assert [response.status_code for response in responses] == [422, 422, 422]
    assert schema["properties"]["name"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 80,
        "title": "Name",
    }


def test_cancelled_task_cannot_be_sealed_through_the_api(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Allowing seal after API cancellation would revive a terminal task."""
    headers = _authorization_header(client, seeded_invite, fake_otp)
    created = client.post(
        "/v1/tasks", json={"name": "Cancel me"}, headers=headers
    ).json()
    cancelled = client.post(f"/v1/tasks/{created['id']}/cancel", headers=headers)
    seal = client.post(f"/v1/tasks/{created['id']}/seal", headers=headers)

    assert cancelled.json()["status"] == "cancelled"
    assert (seal.status_code, seal.json()["detail"]) == (409, "invalid_transition")
