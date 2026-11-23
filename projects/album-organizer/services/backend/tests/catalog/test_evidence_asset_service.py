from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.evidence_assets import (
    EvidenceAssetConflict,
    EvidenceAssetNotFound,
    EvidenceAssetRegistrationService,
)
from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.opc import OpcWorkspace
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User

NOW = datetime(2026, 8, 17, 14, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def clear_evidence_workspaces(
    auth_session_factory: async_sessionmaker[AsyncSession],
):
    async def clear() -> None:
        async with auth_session_factory() as session:
            await session.execute(delete(OpcEvidenceAsset))
            await session.execute(
                delete(OpcWorkspace).where(
                    OpcWorkspace.id.in_(("opc-evidence-a", "opc-evidence-b"))
                )
            )
            await session.commit()

    await clear()
    yield
    await clear()


@dataclass(frozen=True)
class SeededUpload:
    user: User
    other_user: User
    asset: Asset
    blob: StoredBlob


async def _seed_upload(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: AssetStatus = AssetStatus.UPLOADED,
    ready: bool = True,
    media_type: str = "image/jpeg",
    byte_size: int = 128,
    blob_sha256: str = "a" * 64,
    asset_sha256: str | None = None,
) -> SeededUpload:
    user = User(id=uuid4(), phone_e164=f"+86138{str(uuid4().int)[-8:]}")
    other_user = User(id=uuid4(), phone_e164=f"+86139{str(uuid4().int)[-8:]}")
    task = ImportTask(
        id=uuid4(),
        user_id=user.id,
        name="Synthetic evidence upload",
        status=TaskStatus.UPLOADING,
    )
    blob = StoredBlob(
        id=uuid4(),
        user_id=user.id,
        sha256=blob_sha256,
        byte_size=byte_size,
        object_key=f"private/{user.id}/{uuid4()}/original",
        is_ready=ready,
    )
    asset = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        blob_id=blob.id,
        local_asset_id=f"local-{uuid4()}",
        filename="synthetic.jpg",
        media_type=media_type,
        byte_size=byte_size,
        sha256=asset_sha256 or blob_sha256,
        captured_at=NOW,
        status=status,
    )
    async with session_factory() as session:
        session.add_all(
            [
                user,
                other_user,
                OpcWorkspace(id="opc-evidence-a", display_name="Evidence A"),
                OpcWorkspace(id="opc-evidence-b", display_name="Evidence B"),
            ]
        )
        await session.flush()
        session.add_all([task, blob])
        await session.flush()
        session.add(asset)
        await session.commit()
    return SeededUpload(user=user, other_user=other_user, asset=asset, blob=blob)


@pytest.mark.asyncio
async def test_registers_ready_original_and_exact_replay_is_idempotent(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_upload(auth_session_factory)
    service = EvidenceAssetRegistrationService(auth_session_factory)

    first = await service.register(
        opc_id="opc-evidence-a",
        user_id=seeded.user.id,
        source_asset_id=seeded.asset.id,
    )
    replay = await service.register(
        opc_id="opc-evidence-a",
        user_id=seeded.user.id,
        source_asset_id=seeded.asset.id,
    )

    assert replay.id == first.id
    assert first.storage_ref == f"evidence/{first.id}/original"
    assert first.source_object_key == seeded.blob.object_key
    assert first.source_blob_id == seeded.blob.id
    assert first.sha256 == seeded.asset.sha256 == seeded.blob.sha256
    assert first.byte_size == seeded.asset.byte_size == seeded.blob.byte_size
    assert first.media_type == "image/jpeg"
    assert first.captured_at == NOW
    assert first.imported_at == seeded.asset.created_at
    async with auth_session_factory() as session:
        count = await session.scalar(select(func.count(OpcEvidenceAsset.id)))
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "expected_error"),
    (
        ("wrong_user", EvidenceAssetNotFound),
        ("pending", EvidenceAssetNotFound),
        ("failed", EvidenceAssetNotFound),
        ("unsupported", EvidenceAssetNotFound),
        ("blob_not_ready", EvidenceAssetNotFound),
        ("metadata_mismatch", EvidenceAssetNotFound),
        ("media_type", EvidenceAssetNotFound),
        ("oversized", EvidenceAssetNotFound),
    ),
)
async def test_invalid_upload_boundaries_never_create_evidence(
    auth_session_factory: async_sessionmaker[AsyncSession],
    boundary: str,
    expected_error: type[Exception],
) -> None:
    kwargs: dict[str, object] = {}
    if boundary == "pending":
        kwargs["status"] = AssetStatus.PENDING
    elif boundary == "failed":
        kwargs["status"] = AssetStatus.FAILED
    elif boundary == "unsupported":
        kwargs["status"] = AssetStatus.SKIPPED_UNSUPPORTED
    elif boundary == "blob_not_ready":
        kwargs["ready"] = False
    elif boundary == "metadata_mismatch":
        kwargs["asset_sha256"] = "b" * 64
    elif boundary == "media_type":
        kwargs["media_type"] = "image/gif"
    elif boundary == "oversized":
        kwargs["byte_size"] = 104_857_601
    seeded = await _seed_upload(auth_session_factory, **kwargs)  # type: ignore[arg-type]
    user_id = seeded.other_user.id if boundary == "wrong_user" else seeded.user.id

    with pytest.raises(expected_error, match="^asset_not_found$"):
        await EvidenceAssetRegistrationService(auth_session_factory).register(
            opc_id="opc-evidence-a",
            user_id=user_id,
            source_asset_id=seeded.asset.id,
        )

    async with auth_session_factory() as session:
        count = await session.scalar(select(func.count(OpcEvidenceAsset.id)))
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ("captured_at", "asset_sha256", "blob_byte_size", "blob_not_ready", "failed"),
)
async def test_existing_registration_with_changed_source_is_conflict(
    auth_session_factory: async_sessionmaker[AsyncSession],
    boundary: str,
) -> None:
    seeded = await _seed_upload(auth_session_factory)
    service = EvidenceAssetRegistrationService(auth_session_factory)
    registered = await service.register(
        opc_id="opc-evidence-a",
        user_id=seeded.user.id,
        source_asset_id=seeded.asset.id,
    )
    async with auth_session_factory() as session:
        asset = await session.get(Asset, seeded.asset.id)
        blob = await session.get(StoredBlob, seeded.blob.id)
        assert asset is not None
        assert blob is not None
        if boundary == "captured_at":
            asset.captured_at = datetime(2026, 8, 17, 15, tzinfo=UTC)
        elif boundary == "asset_sha256":
            asset.sha256 = "b" * 64
        elif boundary == "blob_byte_size":
            blob.byte_size += 1
        elif boundary == "blob_not_ready":
            blob.is_ready = False
        else:
            asset.status = AssetStatus.FAILED
        await session.commit()

    with pytest.raises(EvidenceAssetConflict, match="^evidence_asset_conflict$"):
        await service.register(
            opc_id="opc-evidence-a",
            user_id=seeded.user.id,
            source_asset_id=seeded.asset.id,
        )

    assert registered.id is not None


@pytest.mark.asyncio
async def test_cross_opc_concurrent_registration_has_one_owner(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_upload(auth_session_factory)
    async with auth_session_factory() as session:
        await session.execute(
            text(
                "CREATE OR REPLACE FUNCTION test_pause_evidence_insert() "
                "RETURNS trigger AS $$ BEGIN PERFORM pg_sleep(0.15); "
                "RETURN NEW; END $$ LANGUAGE plpgsql"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER test_pause_evidence_insert_trigger "
                "BEFORE INSERT ON opc_evidence_assets FOR EACH ROW "
                "EXECUTE FUNCTION test_pause_evidence_insert()"
            )
        )
        await session.commit()
    try:
        results = await asyncio.gather(
            EvidenceAssetRegistrationService(auth_session_factory).register(
                opc_id="opc-evidence-a",
                user_id=seeded.user.id,
                source_asset_id=seeded.asset.id,
            ),
            EvidenceAssetRegistrationService(auth_session_factory).register(
                opc_id="opc-evidence-b",
                user_id=seeded.user.id,
                source_asset_id=seeded.asset.id,
            ),
            return_exceptions=True,
        )
    finally:
        async with auth_session_factory() as session:
            await session.execute(
                text(
                    "DROP TRIGGER IF EXISTS test_pause_evidence_insert_trigger "
                    "ON opc_evidence_assets"
                )
            )
            await session.execute(
                text("DROP FUNCTION IF EXISTS test_pause_evidence_insert()")
            )
            await session.commit()

    successes = [result for result in results if isinstance(result, OpcEvidenceAsset)]
    failures = [
        result for result in results if isinstance(result, EvidenceAssetNotFound)
    ]
    assert len(successes) == 1
    assert len(failures) == 1
    assert str(failures[0]) == "asset_not_found"
    async with auth_session_factory() as session:
        rows = (await session.scalars(select(OpcEvidenceAsset))).all()
    assert len(rows) == 1
    assert rows[0].opc_id in {"opc-evidence-a", "opc-evidence-b"}
