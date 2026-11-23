from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.upload_bindings import UploadBindingInvalidCandidate
from photo_organizer.domain.enums import TaskStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask
from photo_organizer.models.upload_binding import ControlledUploadBinding
from photo_organizer.models.user import User
from photo_organizer.schemas.controlled_upload_candidate import (
    controlled_upload_candidate_id,
)

OPC_PREFIX = "opc-upload-binding-api-"
TASK_NAME = "Upload binding API fixture"
NOW = datetime(2026, 8, 18, 13, tzinfo=UTC)


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _authorization_header(
    client: TestClient, seeded_invite, fake_otp
) -> tuple[dict[str, str], UUID]:
    phone = f"+86137{str(uuid4().int)[-8:]}"
    requested = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "upload-binding-api-test",
        },
    )
    assert (requested.status_code, verified.status_code) == (202, 200)
    token = verified.json()["access_token"]
    user_id = UUID(
        str(_app(client).state.token_service.decode_access_token(token)["sub"])
    )
    return {"Authorization": f"Bearer {token}"}, user_id


def _candidate_json(opc_id: str, *, marker: str = "a") -> dict[str, object]:
    first_id = uuid4()
    second_id = uuid4()
    body: dict[str, object] = {
        "schema_version": "opc-controlled-upload-candidate/1.0",
        "status": "ready_for_authenticated_task_binding",
        "source_readiness_id": f"opc-real-sku-pilot-readiness:{marker * 64}",
        "opc_id": opc_id,
        "opc_authority_state": "requires_authenticated_revalidation",
        "album_product_id": f"album-product-{marker * 8}",
        "source_type": "first_sale_bead",
        "target_chain": "sellable_product_material",
        "data_classification": "opc_owned_product_material_no_customer_data",
        "material_owner": "opc",
        "authorization_basis": "opc_self_owned_product_material",
        "authorized_by": "user:33333333-3333-4333-8333-333333333333",
        "authorized_at": NOW.isoformat().replace("+00:00", "Z"),
        "privacy_review_confirmed": True,
        "customer_data_present": False,
        "items": [
            {
                "local_asset_id": str(first_id),
                "role": "primary",
                "position": 1,
                "filename": f"{first_id}.jpg",
                "media_type": "image/jpeg",
                "byte_size": 123,
                "sha256": "1" * 64,
                "captured_at": NOW.isoformat().replace("+00:00", "Z"),
                "human_privacy_reviewed": True,
                "privacy_metadata_status": "no_sensitive_metadata_detected",
                "retry": False,
            },
            {
                "local_asset_id": str(second_id),
                "role": "detail",
                "position": 2,
                "filename": f"{second_id}.png",
                "media_type": "image/png",
                "byte_size": 456,
                "sha256": "2" * 64,
                "captured_at": NOW.isoformat().replace("+00:00", "Z"),
                "human_privacy_reviewed": True,
                "privacy_metadata_status": "no_sensitive_metadata_detected",
                "retry": False,
            },
        ],
        "claims_not_evaluated": [
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        ],
        "target_task_id": None,
        "task_binding_state": "not_bound",
        "upload_state": "not_started",
        "next_action": "bind_to_verified_opc_upload_task",
        "mall_product_id": None,
        "bead_id": None,
        "publication_state": "not_created",
        "auto_publish": False,
        "shared_market_opt_in": False,
    }
    body["candidate_id"] = controlled_upload_candidate_id(body)
    return body


async def _clear(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        task_ids = select(ImportTask.id).where(ImportTask.name == TASK_NAME)
        task_users = select(ImportTask.user_id).where(ImportTask.name == TASK_NAME)
        await session.execute(delete(Asset).where(Asset.task_id.in_(task_ids)))
        await session.execute(
            delete(StoredBlob).where(StoredBlob.user_id.in_(task_users))
        )
        await session.execute(
            delete(OutboxEvent).where(OutboxEvent.topic == "upload.candidate.bound.v1")
        )
        await session.execute(delete(ControlledUploadBinding))
        await session.execute(delete(ImportTask).where(ImportTask.name == TASK_NAME))
        await session.execute(
            delete(OpcMembership).where(OpcMembership.opc_id.like(f"{OPC_PREFIX}%"))
        )
        await session.execute(
            delete(OpcWorkspace).where(OpcWorkspace.id.like(f"{OPC_PREFIX}%"))
        )
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def clear_upload_binding_api_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    await _clear(auth_session_factory)
    yield
    await _clear(auth_session_factory)


async def _seed_task(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    status: TaskStatus = TaskStatus.WAITING_UPLOAD,
    membership: bool = True,
    opc_id: str | None = None,
) -> tuple[str, UUID]:
    workspace_id = opc_id or f"{OPC_PREFIX}{uuid4().hex}"
    task = ImportTask(user_id=user_id, name=TASK_NAME, status=status)
    async with session_factory() as session:
        session.add(OpcWorkspace(id=workspace_id, display_name="Upload Binding API"))
        await session.flush()
        if membership:
            session.add(
                OpcMembership(opc_id=workspace_id, user_id=user_id, role="owner")
            )
        session.add(task)
        await session.commit()
    return workspace_id, task.id


def _request(task_id: UUID, candidate: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "opc-controlled-upload-binding-request/1.0",
        "task_id": str(task_id),
        "candidate": candidate,
    }


def test_route_requires_bearer_and_current_opc_membership(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, task_id = asyncio.run(
        _seed_task(auth_session_factory, user_id=user_id, membership=False)
    )
    body = _request(task_id, _candidate_json(opc_id))

    missing_bearer = client.post(f"/v1/opcs/{opc_id}/upload-bindings", json=body)
    non_member = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings", json=body, headers=headers
    )

    assert (missing_bearer.status_code, missing_bearer.json()["detail"]) == (
        401,
        "invalid_bearer_token",
    )
    assert (non_member.status_code, non_member.json()["detail"]) == (
        404,
        "opc_not_found",
    )


def test_member_binds_candidate_and_exact_replay_is_idempotent(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, task_id = asyncio.run(_seed_task(auth_session_factory, user_id=user_id))
    body = _request(task_id, _candidate_json(opc_id))

    first = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings", json=body, headers=headers
    )
    replay = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings", json=body, headers=headers
    )

    assert (first.status_code, replay.status_code) == (200, 200)
    assert replay.json() == first.json()
    result = first.json()
    assert result["schema_version"] == "opc-controlled-upload-binding/1.0"
    assert result["opc_id"] == opc_id
    assert result["task_id"] == str(task_id)
    assert result["bound_by"] == f"user:{user_id}"
    assert result["opc_authority_state"] == (
        "verified_by_authenticated_membership_and_task_binding"
    )
    assert result["mall_product_id"] is None
    assert result["bead_id"] is None
    assert result["auto_publish"] is False
    assert result["shared_market_opt_in"] is False

    async def counts() -> tuple[int, int]:
        async with auth_session_factory() as session:
            return (
                int(
                    await session.scalar(select(func.count(ControlledUploadBinding.id)))
                    or 0
                ),
                int(
                    await session.scalar(
                        select(func.count(OutboxEvent.id)).where(
                            OutboxEvent.topic == "upload.candidate.bound.v1"
                        )
                    )
                    or 0
                ),
            )

    assert asyncio.run(counts()) == (1, 1)


def test_bound_candidate_item_can_initiate_only_with_exact_authority_and_metadata(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, task_id = asyncio.run(_seed_task(auth_session_factory, user_id=user_id))
    candidate = _candidate_json(opc_id)
    cast(list[dict[str, object]], candidate["items"])[0]["byte_size"] = 8
    candidate["candidate_id"] = controlled_upload_candidate_id(candidate)
    bound = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings",
        json=_request(task_id, candidate),
        headers=headers,
    )
    assert bound.status_code == 200
    item = cast(list[dict[str, object]], candidate["items"])[0]
    payload = {
        "binding_id": bound.json()["binding_id"],
        "candidate_id": candidate["candidate_id"],
        "local_asset_id": item["local_asset_id"],
        "filename": item["filename"],
        "media_type": item["media_type"],
        "byte_size": item["byte_size"],
        "sha256": item["sha256"],
        "captured_at": item["captured_at"],
    }

    initiated = client.post(
        f"/v1/tasks/{task_id}/assets/initiate",
        json=payload,
        headers=headers,
    )

    assert initiated.status_code == 201
    assert initiated.json()["already_uploaded"] is False


def test_uploaded_bound_item_remains_idempotent_after_task_closes(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, task_id = asyncio.run(_seed_task(auth_session_factory, user_id=user_id))
    candidate = _candidate_json(opc_id)
    cast(list[dict[str, object]], candidate["items"])[0]["byte_size"] = 8
    candidate["candidate_id"] = controlled_upload_candidate_id(candidate)
    bound = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings",
        json=_request(task_id, candidate),
        headers=headers,
    )
    item = cast(list[dict[str, object]], candidate["items"])[0]
    payload = {
        "binding_id": bound.json()["binding_id"],
        "candidate_id": candidate["candidate_id"],
        "local_asset_id": item["local_asset_id"],
        "filename": item["filename"],
        "media_type": item["media_type"],
        "byte_size": item["byte_size"],
        "sha256": item["sha256"],
        "captured_at": item["captured_at"],
    }
    initiated = client.post(
        f"/v1/tasks/{task_id}/assets/initiate", json=payload, headers=headers
    )
    completed = client.post(
        f"/v1/assets/{initiated.json()['asset_id']}/complete",
        json={"parts": [{"part_number": 1, "etag": "bound-etag"}]},
        headers=headers,
    )
    cancelled = client.post(f"/v1/tasks/{task_id}/cancel", headers=headers)

    repeated = client.post(
        f"/v1/tasks/{task_id}/assets/initiate", json=payload, headers=headers
    )

    assert completed.json()["status"] == "uploaded"
    assert cancelled.status_code == 200
    assert repeated.status_code == 201
    assert repeated.json()["already_uploaded"] is True
    assert _app(client).state.upload_service.storage.create_call_count == 1


def test_bound_task_rejects_missing_or_mismatched_candidate_authority_before_storage(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, task_id = asyncio.run(_seed_task(auth_session_factory, user_id=user_id))
    candidate = _candidate_json(opc_id)
    bound = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings",
        json=_request(task_id, candidate),
        headers=headers,
    )
    assert bound.status_code == 200
    item = cast(list[dict[str, object]], candidate["items"])[0]
    base = {
        "local_asset_id": item["local_asset_id"],
        "filename": item["filename"],
        "media_type": item["media_type"],
        "byte_size": item["byte_size"],
        "sha256": item["sha256"],
        "captured_at": item["captured_at"],
    }

    missing = client.post(
        f"/v1/tasks/{task_id}/assets/initiate",
        json=base,
        headers=headers,
    )
    mismatched = client.post(
        f"/v1/tasks/{task_id}/assets/initiate",
        json={
            **base,
            "binding_id": bound.json()["binding_id"],
            "candidate_id": candidate["candidate_id"],
            "sha256": "f" * 64,
        },
        headers=headers,
    )

    assert (missing.status_code, missing.json()["detail"]) == (
        409,
        "upload_binding_required",
    )
    assert (mismatched.status_code, mismatched.json()["detail"]) == (
        409,
        "upload_binding_mismatch",
    )
    assert _app(client).state.upload_service.storage.create_call_count == 0

    async def count_assets() -> int:
        async with auth_session_factory() as session:
            return int(
                await session.scalar(select(func.count()).select_from(Asset)) or 0
            )

    assert asyncio.run(count_assets()) == 0


def test_bound_task_rejects_wrong_binding_identity_and_damaged_snapshot(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, task_id = asyncio.run(_seed_task(auth_session_factory, user_id=user_id))
    candidate = _candidate_json(opc_id)
    bound = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings",
        json=_request(task_id, candidate),
        headers=headers,
    )
    assert bound.status_code == 200
    item = cast(list[dict[str, object]], candidate["items"])[0]
    payload = {
        "binding_id": str(uuid4()),
        "candidate_id": candidate["candidate_id"],
        "local_asset_id": item["local_asset_id"],
        "filename": item["filename"],
        "media_type": item["media_type"],
        "byte_size": item["byte_size"],
        "sha256": item["sha256"],
        "captured_at": item["captured_at"],
    }
    wrong_binding = client.post(
        f"/v1/tasks/{task_id}/assets/initiate",
        json=payload,
        headers=headers,
    )

    async def damage_snapshot() -> None:
        async with auth_session_factory() as session:
            binding = await session.get(
                ControlledUploadBinding, UUID(bound.json()["binding_id"])
            )
            assert binding is not None and binding.candidate_payload is not None
            binding.candidate_payload = {
                **binding.candidate_payload,
                "candidate_id": ("opc-controlled-upload-candidate:" + "0" * 64),
            }
            await session.commit()

    asyncio.run(damage_snapshot())
    payload["binding_id"] = bound.json()["binding_id"]
    damaged = client.post(
        f"/v1/tasks/{task_id}/assets/initiate",
        json=payload,
        headers=headers,
    )

    assert (wrong_binding.status_code, wrong_binding.json()["detail"]) == (
        409,
        "upload_binding_mismatch",
    )
    assert (damaged.status_code, damaged.json()["detail"]) == (
        409,
        "upload_binding_mismatch",
    )
    assert _app(client).state.upload_service.storage.create_call_count == 0


def test_invalid_candidate_is_redacted_without_echoing_input(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, task_id = asyncio.run(_seed_task(auth_session_factory, user_id=user_id))
    candidate = _candidate_json(opc_id)
    candidate["signed_url"] = "https://example.invalid/private?token=do-not-echo"

    response = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings",
        json=_request(task_id, candidate),
        headers=headers,
    )

    assert (response.status_code, response.json()) == (
        422,
        {"detail": "invalid_candidate"},
    )
    assert "do-not-echo" not in response.text
    assert "signed_url" not in response.text


def test_invalid_opc_path_is_the_same_redacted_422(
    client: TestClient,
    seeded_invite,
    fake_otp,
) -> None:
    headers, _ = _authorization_header(client, seeded_invite, fake_otp)
    safe_candidate_opc = f"{OPC_PREFIX}{uuid4().hex}"
    illegal_opc_path = "bad$opc-do-not-echo"

    response = client.post(
        f"/v1/opcs/{illegal_opc_path}/upload-bindings",
        json=_request(uuid4(), _candidate_json(safe_candidate_opc)),
        headers=headers,
    )

    assert (response.status_code, response.json()) == (
        422,
        {"detail": "invalid_candidate"},
    )
    assert "do-not-echo" not in response.text
    assert illegal_opc_path not in response.text


def test_service_level_invalid_candidate_is_a_redacted_422(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, task_id = asyncio.run(_seed_task(auth_session_factory, user_id=user_id))
    application = _app(client)
    original = application.state.upload_binding_service

    class InvalidCandidateService:
        async def bind(self, **_: object) -> None:
            raise UploadBindingInvalidCandidate("do-not-echo-private-value")

    application.state.upload_binding_service = InvalidCandidateService()
    try:
        response = client.post(
            f"/v1/opcs/{opc_id}/upload-bindings",
            json=_request(task_id, _candidate_json(opc_id)),
            headers=headers,
        )
    finally:
        application.state.upload_binding_service = original

    assert (response.status_code, response.json()) == (
        422,
        {"detail": "invalid_candidate"},
    )
    assert "do-not-echo" not in response.text


def test_candidate_opc_mismatch_closed_task_and_cross_user_are_safe(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    opc_id, closed_task_id = asyncio.run(
        _seed_task(
            auth_session_factory,
            user_id=user_id,
            status=TaskStatus.COMPLETED,
        )
    )
    mismatch = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings",
        json=_request(
            closed_task_id,
            _candidate_json(f"{OPC_PREFIX}{uuid4().hex}", marker="b"),
        ),
        headers=headers,
    )
    closed = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings",
        json=_request(closed_task_id, _candidate_json(opc_id, marker="c")),
        headers=headers,
    )

    async def add_other_member() -> tuple[dict[str, str], UUID]:
        other_user = User(phone_e164=f"+86139{str(uuid4().int)[-8:]}")
        async with auth_session_factory() as session:
            session.add(other_user)
            await session.flush()
            session.add(
                OpcMembership(opc_id=opc_id, user_id=other_user.id, role="reviewer")
            )
            await session.commit()
        pair = await _app(client).state.token_service.issue_pair(
            other_user.id, "upload-binding-cross-user"
        )
        return {"Authorization": f"Bearer {pair.access_token}"}, other_user.id

    other_headers, _ = asyncio.run(add_other_member())
    cross_user = client.post(
        f"/v1/opcs/{opc_id}/upload-bindings",
        json=_request(closed_task_id, _candidate_json(opc_id, marker="d")),
        headers=other_headers,
    )

    assert (mismatch.status_code, mismatch.json()["detail"]) == (
        409,
        "candidate_opc_mismatch",
    )
    assert (closed.status_code, closed.json()["detail"]) == (409, "task_not_open")
    assert (cross_user.status_code, cross_user.json()["detail"]) == (
        404,
        "task_not_found",
    )


def test_openapi_exposes_only_the_strict_binding_request_and_response(
    client: TestClient,
) -> None:
    contract = _app(client).openapi()
    operation = contract["paths"]["/v1/opcs/{opc_id}/upload-bindings"]["post"]

    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    response_schema = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert request_schema == {
        "$ref": "#/components/schemas/ControlledUploadBindingRequest"
    }
    assert response_schema == {
        "$ref": "#/components/schemas/ControlledUploadBindingResponse"
    }
    response_contract = contract["components"]["schemas"][
        "ControlledUploadBindingResponse"
    ]
    assert set(response_contract["required"]) >= {
        "binding_id",
        "candidate_id",
        "opc_id",
        "task_id",
        "claims_not_evaluated",
        "mall_product_id",
        "bead_id",
        "auto_publish",
        "shared_market_opt_in",
    }
    expected_errors = {
        "401": "UploadBindingUnauthorizedError",
        "404": "UploadBindingNotFoundError",
        "409": "UploadBindingConflictError",
        "422": "UploadBindingInvalidCandidateError",
    }
    for status_code, schema_name in expected_errors.items():
        error_schema = operation["responses"][status_code]["content"][
            "application/json"
        ]["schema"]
        assert error_schema == {"$ref": f"#/components/schemas/{schema_name}"}

    schemas = contract["components"]["schemas"]
    assert schemas["UploadBindingUnauthorizedError"]["properties"]["detail"] == {
        "type": "string",
        "const": "invalid_bearer_token",
        "title": "Detail",
    }
    assert set(
        schemas["UploadBindingNotFoundError"]["properties"]["detail"]["enum"]
    ) == {"opc_not_found", "task_not_found"}
    assert set(
        schemas["UploadBindingConflictError"]["properties"]["detail"]["enum"]
    ) == {
        "candidate_opc_mismatch",
        "task_not_open",
        "candidate_already_bound",
    }
    assert schemas["UploadBindingInvalidCandidateError"]["properties"]["detail"] == {
        "type": "string",
        "const": "invalid_candidate",
        "title": "Detail",
    }
