from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User


@pytest.mark.asyncio
async def test_task_and_asset_are_tenant_scoped(db_session) -> None:
    """A task asset must remain owned by the task's tenant."""
    user = User(id=uuid4(), phone_e164="+8613800000000")
    task = ImportTask(
        id=uuid4(), user_id=user.id, name="8月看货", status=TaskStatus.WAITING_UPLOAD
    )
    asset = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        local_asset_id="ios:ABC",
        filename="IMG_0001.HEIC",
        media_type="image/heic",
        byte_size=1024,
        sha256="a" * 64,
        status=AssetStatus.PENDING,
    )
    db_session.add_all([user, task, asset])
    await db_session.commit()

    stored_asset = await db_session.scalar(
        select(Asset).where(Asset.task_id == task.id)
    )
    assert stored_asset is not None
    assert stored_asset.user_id == user.id


@pytest.mark.asyncio
async def test_asset_rejects_task_owned_by_another_tenant(db_session) -> None:
    owner = User(id=uuid4(), phone_e164="+8613800000001")
    other_user = User(id=uuid4(), phone_e164="+8613800000002")
    task = ImportTask(
        id=uuid4(),
        user_id=other_user.id,
        name="other",
        status=TaskStatus.WAITING_UPLOAD,
    )
    db_session.add_all([owner, other_user, task])
    await db_session.commit()

    db_session.add(
        Asset(
            id=uuid4(),
            user_id=owner.id,
            task_id=task.id,
            local_asset_id="ios:cross-task",
            filename="IMG_0002.HEIC",
            media_type="image/heic",
            byte_size=1024,
            sha256="b" * 64,
            status=AssetStatus.PENDING,
        )
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


@pytest.mark.asyncio
async def test_asset_rejects_blob_owned_by_another_tenant(db_session) -> None:
    owner = User(id=uuid4(), phone_e164="+8613800000003")
    other_user = User(id=uuid4(), phone_e164="+8613800000004")
    task = ImportTask(
        id=uuid4(), user_id=owner.id, name="owner", status=TaskStatus.WAITING_UPLOAD
    )
    db_session.add_all([owner, other_user])
    await db_session.commit()

    blob = StoredBlob(
        id=uuid4(),
        user_id=other_user.id,
        sha256="c" * 64,
        byte_size=1024,
        object_key="other/original",
    )
    db_session.add_all([task, blob])
    await db_session.commit()

    db_session.add(
        Asset(
            id=uuid4(),
            user_id=owner.id,
            task_id=task.id,
            blob_id=blob.id,
            local_asset_id="ios:cross-blob",
            filename="IMG_0003.HEIC",
            media_type="image/heic",
            byte_size=1024,
            sha256="d" * 64,
            status=AssetStatus.PENDING,
        )
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


@pytest.mark.asyncio
async def test_accepted_asset_rejects_null_sha256(db_session) -> None:
    user = User(id=uuid4(), phone_e164="+8613800000005")
    task = ImportTask(
        id=uuid4(), user_id=user.id, name="task", status=TaskStatus.WAITING_UPLOAD
    )
    db_session.add_all([user, task])
    await db_session.commit()

    db_session.add(
        Asset(
            id=uuid4(),
            user_id=user.id,
            task_id=task.id,
            local_asset_id="ios:null-sha",
            filename="IMG_0004.HEIC",
            media_type="image/heic",
            byte_size=1024,
            sha256=None,
            status=AssetStatus.PENDING,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_accepted_asset_rejects_null_byte_size(db_session) -> None:
    user = User(id=uuid4(), phone_e164="+8613800000006")
    task = ImportTask(
        id=uuid4(), user_id=user.id, name="task", status=TaskStatus.WAITING_UPLOAD
    )
    db_session.add_all([user, task])
    await db_session.commit()

    db_session.add(
        Asset(
            id=uuid4(),
            user_id=user.id,
            task_id=task.id,
            local_asset_id="ios:null-size",
            filename="IMG_0005.HEIC",
            media_type="image/heic",
            byte_size=None,
            sha256="e" * 64,
            status=AssetStatus.PENDING,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_rollback_then_commit_stays_inside_fixture_transaction(
    db_session, db_engine: AsyncEngine
) -> None:
    phone_e164 = f"+861{uuid4().int % 10**10:010d}"
    db_session.add(User(id=uuid4(), phone_e164=phone_e164))
    await db_session.flush()
    await db_session.rollback()
    db_session.add(User(id=uuid4(), phone_e164=phone_e164))
    await db_session.commit()

    async with db_engine.connect() as verification_connection:
        persisted_user = await verification_connection.scalar(
            select(User).where(User.phone_e164 == phone_e164)
        )
    assert persisted_user is None
