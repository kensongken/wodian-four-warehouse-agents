from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fakes.fake_object_storage import FakeObjectStorage
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.snapshots import AlbumProductSnapshotService
from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    ConfirmedSnapshotBatch,
)
from photo_organizer.categorization.review import ClassificationReviewService
from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.review import ClassificationReviewItem
from photo_organizer.models.task import ImportTask
from photo_organizer.storage.object_storage import (
    StoragePermanent,
    StorageTransient,
)

ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_FIXTURE = ROOT / "contracts/fixtures/first-10-sku-categorization-events.json"
CANDIDATE_FIXTURE = (
    ROOT / "contracts/fixtures/first-10-sku-classification-candidates.json"
)
OPC_ID = "synthetic-opc-001"
NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _storage(client: TestClient) -> FakeObjectStorage:
    return cast(FakeObjectStorage, _app(client).state.upload_service.storage)


def _authorization_header(
    client: TestClient,
    seeded_invite,
    fake_otp,
) -> tuple[dict[str, str], UUID]:
    phone = "+8613800099301"
    requested = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "product-material-download-test",
        },
    )
    assert (requested.status_code, verified.status_code) == (202, 200)
    token = verified.json()["access_token"]
    user_id = UUID(
        str(_app(client).state.token_service.decode_access_token(token)["sub"])
    )
    return {"Authorization": f"Bearer {token}"}, user_id


def _snapshot_batch(*, storage_ref: str | None = None) -> ConfirmedSnapshotBatch:
    source = ConfirmedSnapshotBatch.model_validate_json(
        SNAPSHOT_FIXTURE.read_text(encoding="utf-8")
    )
    batch = ConfirmedSnapshotBatch(
        schema_version=source.schema_version,
        batch_id=source.batch_id,
        generated_at=source.generated_at,
        data_classification=source.data_classification,
        events=(source.events[0],),
    )
    if storage_ref is None:
        return batch
    payload = batch.model_dump(mode="json")
    payload["events"][0]["images"][0]["storage_ref"] = storage_ref
    return ConfirmedSnapshotBatch.model_validate(payload)


def _candidate_batch() -> CategorizationBatchResult:
    source = CategorizationBatchResult.model_validate_json(
        CANDIDATE_FIXTURE.read_text(encoding="utf-8")
    )
    return CategorizationBatchResult(
        schema_version=source.schema_version,
        input_batch_id=source.input_batch_id,
        generated_at=source.generated_at,
        events=(source.events[0],),
        failures=(),
    )


@pytest_asyncio.fixture(autouse=True)
async def clear_material_download_api_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async def clear() -> None:
        async with auth_session_factory() as session:
            await session.execute(delete(OpcEvidenceAsset))
            await session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.topic == "album.product.classification.confirmed.v1"
                )
            )
            await session.execute(delete(ClassificationReviewItem))
            await session.execute(delete(ProductClassification))
            await session.execute(delete(AlbumProductSnapshot))
            await session.execute(
                delete(Asset).where(Asset.local_asset_id.like("api-evidence-%"))
            )
            await session.execute(
                delete(ImportTask).where(
                    ImportTask.name == "API product material evidence"
                )
            )
            await session.execute(
                delete(StoredBlob).where(
                    StoredBlob.object_key.like("private/api-product-material/%")
                )
            )
            await session.execute(delete(OpcMembership))
            await session.execute(delete(OpcWorkspace))
            await session.commit()

    await clear()
    yield
    await clear()


async def _seed_material(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    member_user_id: UUID | None,
    register_evidence: bool = False,
) -> tuple[UUID, UUID, str]:
    async with session_factory() as session:
        session.add(OpcWorkspace(id=OPC_ID, display_name="Synthetic OPC"))
        await session.flush()
        if member_user_id is not None:
            session.add(
                OpcMembership(
                    opc_id=OPC_ID,
                    user_id=member_user_id,
                    role="reviewer",
                )
            )
        await session.commit()
    evidence_id = uuid4()
    storage_ref = f"evidence/{evidence_id}/original"
    snapshots = _snapshot_batch(storage_ref=storage_ref if register_evidence else None)
    candidates = _candidate_batch()
    await AlbumProductSnapshotService(session_factory).ingest(snapshots)
    reviews = await ClassificationReviewService(session_factory).ingest(candidates)
    classification = await ClassificationReviewService(
        session_factory,
        clock=lambda: NOW,
    ).confirm(
        opc_id=OPC_ID,
        review_id=reviews[0].id,
        level_1=candidates.events[0].candidates[0].level_1.value,
        level_2=candidates.events[0].candidates[0].level_2.value,
        confirmed_by=(
            f"user:{member_user_id}"
            if member_user_id is not None
            else "opc-reviewer-001"
        ),
    )
    async with session_factory() as session:
        confirmation_event_id = await session.scalar(
            select(OutboxEvent.id).where(
                OutboxEvent.aggregate_id == classification.id,
            )
        )
    assert confirmation_event_id is not None
    image = snapshots.events[0].images[0]
    if not register_evidence:
        return confirmation_event_id, image.asset_id, image.storage_ref
    assert member_user_id is not None
    source_object_key = (
        f"private/api-product-material/{member_user_id}/{image.asset_id}/original"
    )
    task = ImportTask(
        id=uuid4(),
        user_id=member_user_id,
        name="API product material evidence",
        status=TaskStatus.UPLOADING,
    )
    blob = StoredBlob(
        id=uuid4(),
        user_id=member_user_id,
        sha256=image.sha256,
        byte_size=image.byte_size,
        object_key=source_object_key,
        is_ready=True,
    )
    asset = Asset(
        id=image.asset_id,
        user_id=member_user_id,
        task_id=task.id,
        blob_id=blob.id,
        local_asset_id=f"api-evidence-{image.asset_id}",
        filename="api-evidence-original.jpg",
        media_type=image.media_type,
        byte_size=image.byte_size,
        sha256=image.sha256,
        captured_at=image.captured_at,
        status=AssetStatus.UPLOADED,
        created_at=image.imported_at,
        updated_at=image.imported_at,
    )
    evidence = OpcEvidenceAsset(
        id=evidence_id,
        opc_id=OPC_ID,
        source_asset_id=asset.id,
        source_blob_id=blob.id,
        created_by=member_user_id,
        storage_ref=storage_ref,
        source_object_key=source_object_key,
        sha256=image.sha256,
        byte_size=image.byte_size,
        media_type=image.media_type,
        captured_at=image.captured_at,
        imported_at=image.imported_at,
    )
    async with session_factory() as session:
        session.add_all([task, blob])
        await session.flush()
        session.add(asset)
        await session.flush()
        session.add(evidence)
        await session.commit()
    return confirmation_event_id, image.asset_id, source_object_key


def _download_path(
    confirmation_event_id: UUID, asset_id: UUID, *, opc_id: str = OPC_ID
) -> str:
    return (
        f"/v1/opcs/{opc_id}/product-material-manifest/"
        f"{confirmation_event_id}/assets/{asset_id}/download"
    )


def test_member_gets_exact_short_lived_asset_credential_without_logging_it(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    confirmation_event_id, asset_id, storage_ref = asyncio.run(
        _seed_material(
            auth_session_factory,
            member_user_id=user_id,
            register_evidence=True,
        )
    )

    response = client.post(
        _download_path(confirmation_event_id, asset_id),
        headers=headers,
        json={"storage_ref": "evidence/attacker/chosen.jpg"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "opc-product-material-download/1.0",
        "confirmation_event_id": str(confirmation_event_id),
        "asset_id": str(asset_id),
        "download_url": (
            f"https://storage.invalid/{storage_ref}?expires=300"
            "&X-Amz-Signature=fake-download-signature"
        ),
        "expires_seconds": 300,
    }
    assert _storage(client).download_sign_calls == [storage_ref]
    captured = caplog.text
    assert "X-Amz-Signature" not in captured
    assert "evidence/" not in captured
    assert "fake-download-signature" not in captured
    assert headers["Authorization"] not in captured


def test_download_requires_authentication_and_opc_membership(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, _ = _authorization_header(client, seeded_invite, fake_otp)
    confirmation_event_id, asset_id, _ = asyncio.run(
        _seed_material(auth_session_factory, member_user_id=None)
    )
    path = _download_path(confirmation_event_id, asset_id)

    unauthenticated = client.post(path)
    nonmember = client.post(path, headers=headers)

    assert unauthenticated.status_code == 401
    assert (nonmember.status_code, nonmember.json()["detail"]) == (
        404,
        "opc_not_found",
    )
    assert _storage(client).download_sign_call_count == 0


def test_cross_opc_event_and_unknown_asset_are_tenant_neutral(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    confirmation_event_id, asset_id, _ = asyncio.run(
        _seed_material(
            auth_session_factory,
            member_user_id=user_id,
            register_evidence=True,
        )
    )

    async def add_other_membership() -> None:
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id="opc-other", display_name="Other OPC"))
            await session.flush()
            session.add(
                OpcMembership(
                    opc_id="opc-other",
                    user_id=user_id,
                    role="reviewer",
                )
            )
            await session.commit()

    asyncio.run(add_other_membership())
    cross_opc = client.post(
        _download_path(
            confirmation_event_id,
            asset_id,
            opc_id="opc-other",
        ),
        headers=headers,
    )
    unknown_asset = client.post(
        _download_path(confirmation_event_id, uuid4()),
        headers=headers,
    )

    assert (cross_opc.status_code, cross_opc.json()["detail"]) == (
        404,
        "material_not_found",
    )
    assert (unknown_asset.status_code, unknown_asset.json()["detail"]) == (
        404,
        "material_not_found",
    )
    assert _storage(client).download_sign_call_count == 0


@pytest.mark.parametrize(
    ("storage_error", "detail"),
    (
        (StorageTransient, "material_temporarily_unavailable"),
        (StoragePermanent, "material_unavailable"),
    ),
)
def test_download_maps_storage_failures_to_safe_503(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
    storage_error: type[Exception],
    detail: str,
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)
    confirmation_event_id, asset_id, _ = asyncio.run(
        _seed_material(
            auth_session_factory,
            member_user_id=user_id,
            register_evidence=True,
        )
    )
    _storage(client).inject_fault("sign_download", storage_error)

    response = client.post(
        _download_path(confirmation_event_id, asset_id),
        headers=headers,
    )

    assert (response.status_code, response.json()["detail"]) == (503, detail)
    assert "storage_injected" not in response.text


def test_download_rejects_malformed_uuids(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers, user_id = _authorization_header(client, seeded_invite, fake_otp)

    async def seed_membership() -> None:
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id=OPC_ID, display_name="Synthetic OPC"))
            await session.flush()
            session.add(
                OpcMembership(
                    opc_id=OPC_ID,
                    user_id=user_id,
                    role="reviewer",
                )
            )
            await session.commit()

    asyncio.run(seed_membership())

    response = client.post(
        "/v1/opcs/synthetic-opc-001/product-material-manifest/"
        "not-an-event/assets/not-an-asset/download",
        headers=headers,
    )

    assert response.status_code == 422
    assert _storage(client).download_sign_call_count == 0
