from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.group_confirmation import (
    AlbumProductGroupConfirmation,
    ExistingProductConfirmationConflict,
    ExistingProductNotFound,
    OpcOwnerRequired,
    SnapshotVersionConflict,
)
from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask
from photo_organizer.schemas.group_confirmation import (
    ExistingProductNextSnapshotRequest,
    ExistingProductNextSnapshotResponse,
)

OPC_ID = "opc-group-api"
NOW = datetime(2026, 8, 17, 21, tzinfo=UTC)


def test_existing_product_next_snapshot_contracts_are_strict() -> None:
    evidence_ids = (uuid4(), uuid4())
    request = ExistingProductNextSnapshotRequest(
        confirmation_request_id=uuid4(),
        expected_current_snapshot_version=1,
        evidence_asset_ids=evidence_ids,
        source_type="first_sale_bead",
        level_1_candidate="南红",
        level_2_candidate="手串",
    )
    response = ExistingProductNextSnapshotResponse(
        schema_version="opc-existing-product-next-snapshot/1.0",
        confirmation_event_id=request.confirmation_request_id,
        album_product_id="album-product-synthetic-001",
        snapshot_version=2,
        opc_id=OPC_ID,
        evidence_asset_ids=evidence_ids,
        source_type=request.source_type,
        confirmed_by="user:00000000-0000-4000-8000-000000000001",
        confirmed_at=NOW,
        claims_not_evaluated=(
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        ),
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )

    assert request.expected_current_snapshot_version == 1
    assert response.snapshot_version == 2


@pytest.mark.parametrize(
    "patch",
    (
        {"expected_current_snapshot_version": 0},
        {"evidence_asset_ids": ()},
        {"evidence_asset_ids": (uuid4(),)},
        {"evidence_asset_ids": tuple(uuid4() for _ in range(5))},
        {"evidence_asset_ids": (UUID(int=1), UUID(int=1))},
        {"album_product_id": "client-controlled"},
        {"confirmed_by": "attacker"},
    ),
)
def test_existing_product_next_snapshot_request_rejects_unsafe_input(
    patch: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "confirmation_request_id": uuid4(),
        "expected_current_snapshot_version": 1,
        "evidence_asset_ids": (uuid4(), uuid4()),
        "source_type": "first_sale_bead",
        "level_1_candidate": "南红",
        "level_2_candidate": "手串",
    }
    values.update(patch)

    with pytest.raises(ValidationError):
        ExistingProductNextSnapshotRequest.model_validate(values)


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _authorization_header(
    client: TestClient,
    seeded_invite,
    fake_otp,
) -> tuple[dict[str, str], UUID]:
    phone = "+8613800099501"
    requested = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "group-confirmation-api-test",
        },
    )
    assert (requested.status_code, verified.status_code) == (202, 200)
    token = verified.json()["access_token"]
    user_id = UUID(
        str(_app(client).state.token_service.decode_access_token(token)["sub"])
    )
    return {"Authorization": f"Bearer {token}"}, user_id


@pytest_asyncio.fixture(autouse=True)
async def clear_group_confirmation_api_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async def clear() -> None:
        async with auth_session_factory() as session:
            await session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.topic == "album.product.snapshot.confirmed.v1"
                )
            )
            await session.execute(delete(AlbumProductSnapshot))
            await session.execute(delete(OpcEvidenceAsset))
            await session.execute(
                delete(Asset).where(Asset.local_asset_id.like("group-api-%"))
            )
            await session.execute(
                delete(ImportTask).where(ImportTask.name == "Group API fixture")
            )
            await session.execute(
                delete(StoredBlob).where(
                    StoredBlob.object_key.like("private/group-api/%")
                )
            )
            await session.execute(
                delete(OpcMembership).where(OpcMembership.opc_id == OPC_ID)
            )
            await session.execute(delete(OpcWorkspace).where(OpcWorkspace.id == OPC_ID))
            await session.commit()

    await clear()
    yield
    await clear()


async def _seed_registered_group(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
) -> tuple[UUID, UUID]:
    task = ImportTask(
        id=uuid4(),
        user_id=user_id,
        name="Group API fixture",
        status=TaskStatus.UPLOADING,
    )
    async with session_factory() as session:
        session.add(OpcWorkspace(id=OPC_ID, display_name="Group API OPC"))
        await session.flush()
        session.add(OpcMembership(opc_id=OPC_ID, user_id=user_id, role="reviewer"))
        session.add(task)
        await session.flush()
        evidence_ids: list[UUID] = []
        for index in range(2):
            blob = StoredBlob(
                id=uuid4(),
                user_id=user_id,
                sha256=f"{index + 101:064x}",
                byte_size=2000 + index,
                object_key=f"private/group-api/{user_id}/{uuid4()}/original",
                is_ready=True,
            )
            session.add(blob)
            await session.flush()
            asset = Asset(
                id=uuid4(),
                user_id=user_id,
                task_id=task.id,
                blob_id=blob.id,
                local_asset_id=f"group-api-{uuid4()}",
                filename=f"group-api-{index}.jpg",
                media_type="image/jpeg",
                byte_size=blob.byte_size,
                sha256=blob.sha256,
                captured_at=NOW,
                status=AssetStatus.UPLOADED,
            )
            session.add(asset)
            await session.flush()
            evidence = OpcEvidenceAsset(
                id=uuid4(),
                opc_id=OPC_ID,
                source_asset_id=asset.id,
                source_blob_id=blob.id,
                created_by=user_id,
                storage_ref="",
                source_object_key=blob.object_key,
                sha256=blob.sha256,
                byte_size=blob.byte_size,
                media_type="image/jpeg",
                captured_at=asset.captured_at,
                imported_at=asset.created_at,
            )
            evidence.storage_ref = f"evidence/{evidence.id}/original"
            session.add(evidence)
            evidence_ids.append(evidence.id)
        await session.commit()
    return evidence_ids[0], evidence_ids[1]


async def _seed_membership(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
) -> None:
    async with session_factory() as session:
        session.add(OpcWorkspace(id=OPC_ID, display_name="Group API OPC"))
        await session.flush()
        session.add(OpcMembership(opc_id=OPC_ID, user_id=user_id, role="reviewer"))
        await session.commit()


def test_member_confirms_registered_group_idempotently(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    evidence_ids = asyncio.run(
        _seed_registered_group(auth_session_factory, user_id=user_id)
    )
    request_id = uuid4()
    payload = {
        "confirmation_request_id": str(request_id),
        "evidence_asset_ids": [str(value) for value in evidence_ids],
        "source_type": "first_sale_bead",
        "level_1_candidate": "南红",
        "level_2_candidate": "手串",
    }

    first = client.post(
        f"/v1/opcs/{OPC_ID}/album-products/confirm-group",
        headers=headers,
        json=payload,
    )
    replay = client.post(
        f"/v1/opcs/{OPC_ID}/album-products/confirm-group",
        headers=headers,
        json=payload,
    )

    assert (first.status_code, replay.status_code) == (200, 200)
    assert replay.json() == first.json()
    body = first.json()
    assert body["schema_version"] == "opc-album-product-group-confirmation/1.0"
    assert body["confirmation_event_id"] == str(request_id)
    assert body["album_product_id"] == f"album-product-{request_id}"
    assert body["snapshot_version"] == 1
    assert body["opc_id"] == OPC_ID
    assert body["evidence_asset_ids"] == [str(value) for value in evidence_ids]
    assert body["source_type"] == "first_sale_bead"
    assert body["confirmed_by"] == f"user:{user_id}"
    assert body["claims_not_evaluated"] == [
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    ]
    assert body["mall_product_id"] is None
    assert body["bead_id"] is None
    assert body["publication_mode"] == "draft_only"
    assert body["auto_publish"] is False
    assert headers["Authorization"] not in caplog.text
    assert "private/group-api" not in first.text


@pytest.mark.parametrize(
    "payload_patch",
    (
        {"evidence_asset_ids": []},
        {"evidence_asset_ids": [str(uuid4())]},
        {
            "evidence_asset_ids": [
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000001",
            ]
        },
        {"source_type": "unknown"},
        {"confirmed_by": "attacker"},
        {"mall_product_id": "mall-attacker"},
    ),
)
def test_group_confirmation_request_is_strict(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
    payload_patch: dict[str, object],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asyncio.run(_seed_membership(auth_session_factory, user_id=user_id))
    evidence_id = uuid4()
    payload: dict[str, object] = {
        "confirmation_request_id": str(uuid4()),
        "evidence_asset_ids": [str(evidence_id), str(uuid4())],
        "source_type": "first_sale_bead",
        "level_1_candidate": "无法判断",
        "level_2_candidate": "无法判断",
    }
    payload.update(payload_patch)

    response = client.post(
        f"/v1/opcs/{OPC_ID}/album-products/confirm-group",
        headers=headers,
        json=payload,
    )

    assert response.status_code == 422


def test_group_confirmation_requires_authentication_and_membership(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, _ = _authorization_header(client, seeded_invite, fake_otp)

    async def seed_workspace() -> None:
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id=OPC_ID, display_name="Group API OPC"))
            await session.commit()

    asyncio.run(seed_workspace())
    payload = {
        "confirmation_request_id": str(uuid4()),
        "evidence_asset_ids": [str(uuid4()), str(uuid4())],
        "source_type": "first_sale_bead",
        "level_1_candidate": "无法判断",
        "level_2_candidate": "无法判断",
    }
    path = f"/v1/opcs/{OPC_ID}/album-products/confirm-group"

    unauthenticated = client.post(path, json=payload)
    nonmember = client.post(path, headers=headers, json=payload)

    assert unauthenticated.status_code == 401
    assert (nonmember.status_code, nonmember.json()["detail"]) == (
        404,
        "opc_not_found",
    )


def test_group_confirmation_openapi_declares_unique_evidence_ids(
    client: TestClient,
) -> None:
    schema = _app(client).openapi()["components"]["schemas"][
        "AlbumProductGroupConfirmationRequest"
    ]

    assert schema["properties"]["evidence_asset_ids"]["uniqueItems"] is True


def test_group_confirmation_maps_missing_and_conflict_safely(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    evidence_ids = asyncio.run(
        _seed_registered_group(auth_session_factory, user_id=user_id)
    )
    request_id = uuid4()
    path = f"/v1/opcs/{OPC_ID}/album-products/confirm-group"
    payload = {
        "confirmation_request_id": str(request_id),
        "evidence_asset_ids": [str(value) for value in evidence_ids],
        "source_type": "first_sale_bead",
        "level_1_candidate": "南红",
        "level_2_candidate": "手串",
    }
    first = client.post(path, headers=headers, json=payload)
    payload["source_type"] = "legacy_material"
    conflict = client.post(path, headers=headers, json=payload)
    missing_payload = {
        **payload,
        "confirmation_request_id": str(uuid4()),
        "evidence_asset_ids": [str(uuid4()), str(uuid4())],
    }
    missing = client.post(path, headers=headers, json=missing_payload)

    assert first.status_code == 200
    assert (conflict.status_code, conflict.json()["detail"]) == (
        409,
        "group_confirmation_conflict",
    )
    assert (missing.status_code, missing.json()["detail"]) == (
        404,
        "evidence_group_not_found",
    )


class _NextSnapshotStub:
    def __init__(self, outcome: AlbumProductGroupConfirmation | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def confirm_next_snapshot(self, **kwargs: object):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    (
        (OpcOwnerRequired("opc_owner_required"), 403, "opc_owner_required"),
        (
            ExistingProductNotFound("existing_product_not_found"),
            404,
            "existing_product_not_found",
        ),
        (
            SnapshotVersionConflict("snapshot_version_conflict"),
            409,
            "snapshot_version_conflict",
        ),
        (
            ExistingProductConfirmationConflict(
                "existing_product_confirmation_conflict"
            ),
            409,
            "existing_product_confirmation_conflict",
        ),
    ),
)
def test_next_snapshot_endpoint_maps_stable_failures(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
    error: Exception,
    expected_status: int,
    expected_detail: str,
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asyncio.run(_seed_membership(auth_session_factory, user_id=user_id))
    stub = _NextSnapshotStub(error)
    _app(client).state.group_confirmation_service = stub

    response = client.post(
        f"/v1/opcs/{OPC_ID}/album-products/album-product-1/confirm-next-snapshot",
        headers=headers,
        json={
            "confirmation_request_id": str(uuid4()),
            "expected_current_snapshot_version": 1,
            "evidence_asset_ids": [str(uuid4()), str(uuid4())],
            "source_type": "first_sale_bead",
            "level_1_candidate": "南红",
            "level_2_candidate": "手串",
        },
    )

    assert (response.status_code, response.json()["detail"]) == (
        expected_status,
        expected_detail,
    )


def test_next_snapshot_endpoint_returns_versioned_draft_response(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    asyncio.run(_seed_membership(auth_session_factory, user_id=user_id))
    evidence_ids = (uuid4(), uuid4())
    event_id = uuid4()
    stub = _NextSnapshotStub(
        AlbumProductGroupConfirmation(
            confirmation_event_id=event_id,
            album_product_id="album-product-1",
            snapshot_version=2,
            opc_id=OPC_ID,
            evidence_asset_ids=evidence_ids,
            source_type="first_sale_bead",
            confirmed_by=f"user:{user_id}",
            confirmed_at=NOW,
        )
    )
    _app(client).state.group_confirmation_service = stub

    response = client.post(
        f"/v1/opcs/{OPC_ID}/album-products/album-product-1/confirm-next-snapshot",
        headers=headers,
        json={
            "confirmation_request_id": str(event_id),
            "expected_current_snapshot_version": 1,
            "evidence_asset_ids": [str(value) for value in evidence_ids],
            "source_type": "first_sale_bead",
            "level_1_candidate": "南红",
            "level_2_candidate": "手串",
        },
    )

    assert response.status_code == 200
    assert response.json()["snapshot_version"] == 2
    assert response.json()["publication_mode"] == "draft_only"
    assert response.json()["auto_publish"] is False
    assert stub.calls[0]["album_product_id"] == "album-product-1"
