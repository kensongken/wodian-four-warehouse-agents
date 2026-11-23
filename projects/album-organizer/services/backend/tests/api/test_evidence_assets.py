from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fakes.fake_object_storage import FakeObjectStorage
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User

OPC_ID = "opc-evidence-api"
NOW = datetime(2026, 8, 17, 16, tzinfo=UTC)


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _storage(client: TestClient) -> FakeObjectStorage:
    return cast(FakeObjectStorage, _app(client).state.upload_service.storage)


def _authorization_header(
    client: TestClient,
    seeded_invite,
    fake_otp,
) -> tuple[dict[str, str], UUID]:
    phone = "+8613800099401"
    requested = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "evidence-asset-api-test",
        },
    )
    assert (requested.status_code, verified.status_code) == (202, 200)
    token = verified.json()["access_token"]
    user_id = UUID(
        str(_app(client).state.token_service.decode_access_token(token)["sub"])
    )
    return {"Authorization": f"Bearer {token}"}, user_id


@pytest_asyncio.fixture(autouse=True)
async def clear_evidence_api_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async def clear() -> None:
        async with auth_session_factory() as session:
            await session.execute(delete(OpcEvidenceAsset))
            await session.execute(
                delete(OpcMembership).where(OpcMembership.opc_id == OPC_ID)
            )
            await session.execute(delete(OpcWorkspace).where(OpcWorkspace.id == OPC_ID))
            await session.commit()

    await clear()
    yield
    await clear()


async def _seed_upload(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    member_user_id: UUID | None,
    asset_user_id: UUID,
    status: AssetStatus = AssetStatus.UPLOADED,
    ready: bool = True,
) -> tuple[Asset, StoredBlob]:
    async with session_factory() as session:
        session.add(OpcWorkspace(id=OPC_ID, display_name="Evidence API OPC"))
        await session.flush()
        if member_user_id is not None:
            session.add(
                OpcMembership(
                    opc_id=OPC_ID,
                    user_id=member_user_id,
                    role="reviewer",
                )
            )
            await session.flush()
        if await session.get(User, asset_user_id) is None:
            session.add(
                User(
                    id=asset_user_id,
                    phone_e164=f"+86137{str(asset_user_id.int)[-8:]}",
                )
            )
        await session.flush()
        task = ImportTask(
            id=uuid4(),
            user_id=asset_user_id,
            name="Evidence API upload",
            status=TaskStatus.UPLOADING,
        )
        blob = StoredBlob(
            id=uuid4(),
            user_id=asset_user_id,
            sha256="c" * 64,
            byte_size=456,
            object_key=f"private/{asset_user_id}/{uuid4()}/original",
            is_ready=ready,
        )
        session.add_all([task, blob])
        await session.flush()
        asset = Asset(
            id=uuid4(),
            user_id=asset_user_id,
            task_id=task.id,
            blob_id=blob.id,
            local_asset_id=f"api-{uuid4()}",
            filename="synthetic-api.jpg",
            media_type="image/jpeg",
            byte_size=blob.byte_size,
            sha256=blob.sha256,
            captured_at=NOW,
            status=status,
        )
        session.add(asset)
        await session.commit()
        return asset, blob


def test_member_registers_ready_original_idempotently_without_object_key_leak(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asset, blob = asyncio.run(
        _seed_upload(
            auth_session_factory,
            member_user_id=user_id,
            asset_user_id=user_id,
        )
    )

    first = client.post(
        f"/v1/opcs/{OPC_ID}/evidence-assets",
        headers=headers,
        json={"source_asset_id": str(asset.id)},
    )
    replay = client.post(
        f"/v1/opcs/{OPC_ID}/evidence-assets",
        headers=headers,
        json={"source_asset_id": str(asset.id)},
    )

    assert (first.status_code, replay.status_code) == (200, 200)
    assert replay.json() == first.json()
    body = first.json()
    assert set(body) == {
        "schema_version",
        "evidence_asset_id",
        "opc_id",
        "source_asset_id",
        "storage_ref",
        "sha256",
        "byte_size",
        "media_type",
        "captured_at",
        "imported_at",
    }
    assert body["schema_version"] == "opc-evidence-asset/1.0"
    assert body["opc_id"] == OPC_ID
    assert body["source_asset_id"] == str(asset.id)
    assert body["storage_ref"] == f"evidence/{body['evidence_asset_id']}/original"
    assert body["sha256"] == asset.sha256
    assert body["byte_size"] == asset.byte_size
    assert body["media_type"] == asset.media_type
    assert body["captured_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert blob.object_key not in first.text
    assert _storage(client).operation_log == []
    captured = caplog.text
    assert blob.object_key not in captured
    assert body["storage_ref"] not in captured
    assert headers["Authorization"] not in captured
    assert body["sha256"] not in captured


def test_registration_requires_authentication_and_membership(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asset, _ = asyncio.run(
        _seed_upload(
            auth_session_factory,
            member_user_id=None,
            asset_user_id=user_id,
        )
    )
    path = f"/v1/opcs/{OPC_ID}/evidence-assets"

    unauthenticated = client.post(path, json={"source_asset_id": str(asset.id)})
    nonmember = client.post(
        path,
        headers=headers,
        json={"source_asset_id": str(asset.id)},
    )

    assert unauthenticated.status_code == 401
    assert (nonmember.status_code, nonmember.json()["detail"]) == (
        404,
        "opc_not_found",
    )


@pytest.mark.parametrize("boundary", ("wrong_user", "pending", "blob_not_ready"))
def test_invalid_source_is_tenant_neutral(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
    boundary: str,
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asset_user_id = uuid4() if boundary == "wrong_user" else user_id
    asset, _ = asyncio.run(
        _seed_upload(
            auth_session_factory,
            member_user_id=user_id,
            asset_user_id=asset_user_id,
            status=(
                AssetStatus.PENDING if boundary == "pending" else AssetStatus.UPLOADED
            ),
            ready=boundary != "blob_not_ready",
        )
    )

    response = client.post(
        f"/v1/opcs/{OPC_ID}/evidence-assets",
        headers=headers,
        json={"source_asset_id": str(asset.id)},
    )

    assert (response.status_code, response.json()["detail"]) == (
        404,
        "asset_not_found",
    )


def test_request_rejects_caller_supplied_evidence_and_commerce_identity(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asset, _ = asyncio.run(
        _seed_upload(
            auth_session_factory,
            member_user_id=user_id,
            asset_user_id=user_id,
        )
    )

    response = client.post(
        f"/v1/opcs/{OPC_ID}/evidence-assets",
        headers=headers,
        json={
            "source_asset_id": str(asset.id),
            "storage_ref": "evidence/attacker/original",
            "source_object_key": "private/attacker",
            "created_by": str(uuid4()),
            "mall_product_id": "mall-attacker",
            "bead_id": "bead-attacker",
        },
    )

    assert response.status_code == 422


def test_changed_registered_source_is_stable_conflict(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asset, _ = asyncio.run(
        _seed_upload(
            auth_session_factory,
            member_user_id=user_id,
            asset_user_id=user_id,
        )
    )
    path = f"/v1/opcs/{OPC_ID}/evidence-assets"
    registered = client.post(
        path,
        headers=headers,
        json={"source_asset_id": str(asset.id)},
    )

    async def mutate() -> None:
        async with auth_session_factory() as session:
            source = await session.get(Asset, asset.id)
            assert source is not None
            source.captured_at = datetime(2026, 8, 17, 17, tzinfo=UTC)
            await session.commit()

    asyncio.run(mutate())
    conflict = client.post(
        path,
        headers=headers,
        json={"source_asset_id": str(asset.id)},
    )

    assert registered.status_code == 200
    assert (conflict.status_code, conflict.json()["detail"]) == (
        409,
        "evidence_asset_conflict",
    )
