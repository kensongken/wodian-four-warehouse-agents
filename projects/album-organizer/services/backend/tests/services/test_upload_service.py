import asyncio
import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fakes.fake_object_storage import FakeObjectStorage
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.domain.limits import MAX_TASK_ACCEPTED_ASSETS
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.cleanup_job import StorageCleanupJob
from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User
from photo_organizer.repositories.asset_repository import AssetRepository
from photo_organizer.schemas.assets import AssetInitRequest, CompletedPart
from photo_organizer.services.task_service import TaskService
from photo_organizer.services.upload_service import UploadConflict, UploadService
from photo_organizer.storage.object_storage import (
    MultipartPart,
    ObjectHead,
    StorageNotFound,
    StoragePermanent,
    StorageTransient,
)


@pytest_asyncio.fixture
async def service(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> UploadService:
    return UploadService(auth_session_factory, FakeObjectStorage())


@pytest_asyncio.fixture
async def ready_blob(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> StoredBlob:
    user = User(id=uuid4(), phone_e164="+8613800012001")
    blob = StoredBlob(
        id=uuid4(),
        user_id=user.id,
        sha256="a" * 64,
        byte_size=8,
        object_key="private/ready",
        is_ready=True,
    )
    task = ImportTask(id=uuid4(), user_id=user.id, name="Dedup")
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add_all([task, blob])
        await session.commit()
    return blob


@pytest_asyncio.fixture
async def task(
    auth_session_factory: async_sessionmaker[AsyncSession], ready_blob: StoredBlob
) -> ImportTask:
    from sqlalchemy import select

    async with auth_session_factory() as session:
        task = await session.scalar(
            select(ImportTask).where(ImportTask.user_id == ready_blob.user_id)
        )
        assert task is not None
        return task


@pytest_asyncio.fixture
async def active_upload(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> Asset:
    user = User(id=uuid4(), phone_e164="+8613800012002")
    task = ImportTask(
        id=uuid4(), user_id=user.id, name="Upload", status=TaskStatus.UPLOADING
    )
    blob = StoredBlob(
        id=uuid4(),
        user_id=user.id,
        sha256="b" * 64,
        byte_size=8,
        object_key="private/active",
        provider_upload_id="upload-1",
    )
    asset = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        blob_id=blob.id,
        local_asset_id="android:43",
        filename="IMG.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="b" * 64,
        status=AssetStatus.UPLOADING,
    )
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add_all([task, blob, asset])
        await session.commit()
    return asset


@pytest.mark.asyncio
async def test_initiate_returns_existing_blob_without_upload(
    service: UploadService, ready_blob: StoredBlob, task: ImportTask
) -> None:
    """Creating another multipart upload instead of tenant-local reuse must fail."""
    result = await service.initiate(
        task.user_id,
        task.id,
        AssetInitRequest(
            local_asset_id="android:42",
            filename="IMG.jpg",
            media_type="image/jpeg",
            byte_size=ready_blob.byte_size,
            sha256=ready_blob.sha256,
        ),
    )

    assert result.already_uploaded is True
    assert result.asset.status == AssetStatus.UPLOADED
    assert result.provider_upload_id is None


@pytest.mark.asyncio
async def test_complete_is_idempotent(
    service: UploadService, active_upload: Asset
) -> None:
    """Calling provider completion after an asset is terminal must fail this test."""
    service.storage.seed_upload("private/active")
    first = await service.complete(
        active_upload.user_id,
        active_upload.id,
        [CompletedPart(part_number=1, etag="etag")],
    )
    second = await service.complete(
        active_upload.user_id,
        active_upload.id,
        [CompletedPart(part_number=1, etag="etag")],
    )

    assert second.asset.id == first.asset.id
    assert service.storage.complete_call_count == 1


@pytest.mark.asyncio
async def test_two_service_instances_claim_completion_once(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Replacing a durable completion claim with a process-local lock duplicates S3 work."""
    storage = FakeObjectStorage(complete_delay_seconds=0.02)
    storage.seed_upload("private/active")
    first_service = UploadService(auth_session_factory, storage)
    second_service = UploadService(auth_session_factory, storage)
    parts = [CompletedPart(part_number=1, etag="etag")]

    first, second = await __import__("asyncio").gather(
        first_service.complete(active_upload.user_id, active_upload.id, parts),
        second_service.complete(active_upload.user_id, active_upload.id, parts),
    )

    assert first.asset.id == second.asset.id
    assert storage.complete_call_count == 1


@pytest.mark.asyncio
async def test_same_digest_inflight_uploads_are_independent(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A unique non-ready blob digest incorrectly turns parallel uploads into 500s."""
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    user = User(id=uuid4(), phone_e164="+8613800012010")
    first_task = ImportTask(id=uuid4(), user_id=user.id, name="First")
    second_task = ImportTask(id=uuid4(), user_id=user.id, name="Second")
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add_all([first_task, second_task])
        await session.commit()

    request = AssetInitRequest(
        local_asset_id="same",
        filename="IMG.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="f" * 64,
    )
    first, second = await __import__("asyncio").gather(
        service.initiate(user.id, first_task.id, request),
        service.initiate(user.id, second_task.id, request),
    )

    assert first.provider_upload_id is not None
    assert second.provider_upload_id is not None
    assert first.asset.blob_id != second.asset.blob_id


@pytest.mark.asyncio
async def test_cleanup_processor_retries_transient_abort(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Dropping failed cleanup work would permanently leak provider multipart uploads."""
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    async with auth_session_factory() as session:
        job = StorageCleanupJob(
            user_id=active_upload.user_id,
            blob_id=active_upload.blob_id,
            operation="abort_multipart",
            object_key="private/active",
            provider_upload_id="upload-1",
        )
        session.add(job)
        await session.commit()

    await service.process_cleanup_jobs(active_upload.user_id)
    async with auth_session_factory() as session:
        remaining = await session.get(StorageCleanupJob, job.id)

    assert remaining is None


@pytest.mark.asyncio
async def test_live_completion_heartbeat_prevents_second_service_overlap(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """A lease stolen during a live provider call would consume multipart twice."""
    storage = FakeObjectStorage(complete_delay_seconds=0.12)
    storage.seed_upload("private/active")
    first = UploadService(auth_session_factory, storage, completion_lease_seconds=0.04)
    second = UploadService(auth_session_factory, storage, completion_lease_seconds=0.04)
    parts = [CompletedPart(part_number=1, etag="etag")]

    running = asyncio.create_task(
        first.complete(active_upload.user_id, active_upload.id, parts)
    )
    await asyncio.sleep(0.07)
    other = await second.complete(active_upload.user_id, active_upload.id, parts)
    owner = await running

    assert other.asset.id == owner.asset.id
    assert storage.complete_call_count == 1


@pytest.mark.asyncio
async def test_completed_provider_reconciles_when_claim_changes_before_finalize(
    auth_session_factory: async_sessionmaker[AsyncSession],
    active_upload: Asset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-provider lease handoff must converge instead of rejecting success."""
    storage = FakeObjectStorage()
    storage.seed_upload("private/active")
    first = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)
    second = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)
    finalize_started = asyncio.Event()
    allow_finalize = asyncio.Event()
    original_finalize = first._finalize_completion

    async def delayed_finalize(*args: object, **kwargs: object) -> Asset:
        finalize_started.set()
        await allow_finalize.wait()
        return await original_finalize(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(first, "_finalize_completion", delayed_finalize)
    running = asyncio.create_task(
        first.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await finalize_started.wait()
    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, active_upload.blob_id)
        assert blob is not None
        blob.completion_claimed_at = datetime(2000, 1, 1, tzinfo=UTC)
        await session.commit()
    replacement = await second._claim_completion(
        active_upload.user_id, active_upload.id
    )
    assert isinstance(replacement, tuple)
    allow_finalize.set()

    completed = await running

    assert completed.asset.status == AssetStatus.UPLOADED
    assert storage.complete_call_count == 1


@pytest.mark.asyncio
async def test_expired_crashed_completion_claim_is_reclaimed_and_completed(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """A dead claimant with no heartbeat must not leave an asset permanently uploading."""
    storage = FakeObjectStorage()
    storage.seed_upload("private/active")
    service = UploadService(
        auth_session_factory, storage, completion_lease_seconds=0.01
    )
    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, active_upload.blob_id)
        assert blob is not None
        blob.completion_claim_id = "dead"
        blob.completion_generation = 7
        blob.completion_claimed_at = datetime(2000, 1, 1, tzinfo=UTC)
        await session.commit()

    completed = await service.complete(
        active_upload.user_id,
        active_upload.id,
        [CompletedPart(part_number=1, etag="etag")],
    )
    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, active_upload.blob_id)

    assert completed.asset.status == AssetStatus.UPLOADED
    assert blob is not None and blob.completion_generation == 8
    assert storage.complete_call_count == 1


@pytest.mark.asyncio
async def test_stale_fenced_owner_cannot_finalize_after_reclaim(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Accepting an old token after a newer generation would let a stale worker win."""
    service = UploadService(
        auth_session_factory, FakeObjectStorage(), completion_lease_seconds=0.01
    )
    first = await service._claim_completion(active_upload.user_id, active_upload.id)
    assert isinstance(first, tuple)
    token, generation, *_ = first
    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, active_upload.blob_id)
        assert blob is not None
        blob.completion_claimed_at = datetime(2000, 1, 1, tzinfo=UTC)
        await session.commit()
    second = await service._claim_completion(active_upload.user_id, active_upload.id)
    assert isinstance(second, tuple)

    with pytest.raises(Exception, match="upload_not_active"):
        await service._finalize_completion(
            active_upload.user_id,
            active_upload.id,
            token,
            generation,
            ObjectHead(8, "etag", None),
            [],
        )


@pytest.mark.asyncio
async def test_existing_head_reconciles_after_reclaim_without_second_provider_complete(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """NoSuchUpload after a prior successful completion must converge from object head."""
    storage = FakeObjectStorage()
    storage.seed_upload("private/active")
    await storage.complete_multipart(
        "private/active", "upload-1", [MultipartPart(1, "etag")]
    )
    service = UploadService(
        auth_session_factory, storage, completion_lease_seconds=0.01
    )
    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, active_upload.blob_id)
        assert blob is not None
        blob.completion_claim_id = "dead"
        blob.completion_generation = 2
        blob.completion_claimed_at = datetime(2000, 1, 1, tzinfo=UTC)
        await session.commit()

    completed = await service.complete(
        active_upload.user_id,
        active_upload.id,
        [CompletedPart(part_number=1, etag="etag")],
    )

    assert completed.asset.status == AssetStatus.UPLOADED
    assert storage.complete_call_count == 1


async def _two_same_digest_uploads(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[User, ImportTask, ImportTask]:
    user = User(id=uuid4(), phone_e164="+8613800012020")
    first_task = ImportTask(id=uuid4(), user_id=user.id, name="First")
    second_task = ImportTask(id=uuid4(), user_id=user.id, name="Second")
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add_all([first_task, second_task])
        await session.commit()
    return user, first_task, second_task


def _same_digest_request(local_asset_id: str) -> AssetInitRequest:
    return AssetInitRequest(
        local_asset_id=local_asset_id,
        filename="IMG.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="d" * 64,
    )


@pytest.mark.asyncio
async def test_sequential_ready_digest_winner_repoints_loser_and_cleans_object(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The later same-digest upload must reuse the sole READY winner after completion."""
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    user, first_task, second_task = await _two_same_digest_uploads(auth_session_factory)
    first = await service.initiate(
        user.id, first_task.id, _same_digest_request("first")
    )
    second = await service.initiate(
        user.id, second_task.id, _same_digest_request("second")
    )
    parts = [CompletedPart(part_number=1, etag="etag")]

    await service.complete(user.id, first.asset.id, parts)
    completed_second = await service.complete(user.id, second.asset.id, parts)

    async with auth_session_factory() as session:
        ready = list(
            (
                await session.scalars(
                    select(StoredBlob).where(
                        StoredBlob.user_id == user.id,
                        StoredBlob.sha256 == "d" * 64,
                        StoredBlob.is_ready.is_(True),
                    )
                )
            ).all()
        )
        loser = await session.get(StoredBlob, second.asset.blob_id)
        jobs = list((await session.scalars(select(StorageCleanupJob))).all())

    assert len(ready) == 1
    assert completed_second.asset.blob_id == ready[0].id
    assert loser is not None and loser.cleanup_pending is True
    assert len(jobs) == 1 and jobs[0].operation == "delete_object"
    assert jobs[0].object_key == loser.object_key

    await service.process_cleanup_jobs(user.id)
    async with auth_session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(StorageCleanupJob))
            == 0
        )
        loser = await session.get(StoredBlob, second.asset.blob_id)
    assert loser is not None and loser.cleanup_pending is False
    with pytest.raises(StorageNotFound):
        await storage.head_object(jobs[0].object_key)


@pytest.mark.asyncio
async def test_concurrent_ready_digest_winner_is_serialized_and_loser_is_cleaned(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The digest transaction lock must make two completion workers elect one READY blob."""
    storage = FakeObjectStorage(complete_delay_seconds=0.02)
    first_service = UploadService(auth_session_factory, storage)
    second_service = UploadService(auth_session_factory, storage)
    user, first_task, second_task = await _two_same_digest_uploads(auth_session_factory)
    first = await first_service.initiate(
        user.id, first_task.id, _same_digest_request("first")
    )
    second = await second_service.initiate(
        user.id, second_task.id, _same_digest_request("second")
    )
    parts = [CompletedPart(part_number=1, etag="etag")]

    one, two = await asyncio.gather(
        first_service.complete(user.id, first.asset.id, parts),
        second_service.complete(user.id, second.asset.id, parts),
    )

    async with auth_session_factory() as session:
        ready = list(
            (
                await session.scalars(
                    select(StoredBlob).where(
                        StoredBlob.user_id == user.id,
                        StoredBlob.sha256 == "d" * 64,
                        StoredBlob.is_ready.is_(True),
                    )
                )
            ).all()
        )
        jobs = list((await session.scalars(select(StorageCleanupJob))).all())

    assert one.asset.status == two.asset.status == AssetStatus.UPLOADED
    assert len(ready) == 1
    assert {one.asset.blob_id, two.asset.blob_id} == {ready[0].id}
    assert len(jobs) == 1 and jobs[0].operation == "delete_object"
    await first_service.process_cleanup_jobs(user.id)
    async with auth_session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(StorageCleanupJob))
            == 0
        )


async def _task_with_accepted_assets(
    auth_session_factory: async_sessionmaker[AsyncSession], count: int
) -> tuple[User, ImportTask]:
    user = User(id=uuid4(), phone_e164=f"+8613{uuid4().int % 10**8:08d}")
    task = ImportTask(
        id=uuid4(),
        user_id=user.id,
        name="At capacity",
        status=TaskStatus.UPLOADING,
        total_assets=count,
    )
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add(task)
        await session.flush()
        await session.execute(
            text(
                """
                INSERT INTO assets (
                    id,
                    user_id,
                    task_id,
                    local_asset_id,
                    filename,
                    media_type,
                    byte_size,
                    sha256,
                    status
                )
                SELECT
                    md5(CAST(CAST(:task_id AS uuid) AS text) || ':' || series)::uuid,
                    CAST(:user_id AS uuid),
                    CAST(:task_id AS uuid),
                    'prior:' || (series - 1),
                    'prior.jpg',
                    'image/jpeg',
                    8,
                    lpad(to_hex(series - 1), 64, '0'),
                    'uploaded'
                FROM generate_series(1, :count) AS series
                """
            ),
            {"user_id": user.id, "task_id": task.id, "count": count},
        )
        await session.commit()
    return user, task


async def _assert_initialization_provider_id_is_durably_owned(
    session_factory: async_sessionmaker[AsyncSession],
    storage: FakeObjectStorage,
    task_id: UUID,
) -> None:
    async with session_factory() as session:
        asset = await session.scalar(select(Asset).where(Asset.task_id == task_id))
        assert asset is not None and asset.blob_id is not None
        blob = await session.get(StoredBlob, asset.blob_id)
        assert blob is not None
        cleanup_ids = set(
            (
                await session.scalars(
                    select(StorageCleanupJob.provider_upload_id).where(
                        StorageCleanupJob.user_id == asset.user_id,
                        StorageCleanupJob.object_key == blob.object_key,
                        StorageCleanupJob.operation == "abort_multipart",
                    )
                )
            ).all()
        )
    active_ids = {blob.provider_upload_id} if blob.provider_upload_id else set()
    provider_ids = storage.provider_upload_ids_for(blob.object_key)
    assert provider_ids <= active_ids | cleanup_ids


class _ThreadedCreateObjectStorage(FakeObjectStorage):
    """Mirror S3's cancellation-independent `asyncio.to_thread` creation."""

    def __init__(self) -> None:
        super().__init__()
        self.thread_create_started = threading.Event()
        self.thread_create_release = threading.Event()
        self.thread_create_finished = threading.Event()

    async def create_multipart(self, object_key: str, content_type: str) -> str:
        return await asyncio.to_thread(
            self._create_multipart_blocking, object_key, content_type
        )

    def _create_multipart_blocking(self, object_key: str, content_type: str) -> str:
        self.create_call_count += 1
        self.operation_log.append(("create", object_key))
        self.thread_create_started.set()
        self.thread_create_release.wait()
        upload_id = f"upload-{self._next_upload}"
        self._next_upload += 1
        self._uploads[upload_id] = (object_key, content_type)
        self.thread_create_finished.set()
        return upload_id


@pytest.mark.asyncio
async def test_owned_cancellation_waits_for_threaded_provider_creation_and_exact_id_settlement(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = _ThreadedCreateObjectStorage()
    service = UploadService(auth_session_factory, storage)
    request = _same_digest_request("threaded-create-cancel")

    caller = asyncio.create_task(service.initiate(user.id, task.id, request))
    await asyncio.to_thread(storage.thread_create_started.wait)
    owned = next(iter(service._owned_settlement_tasks))
    owned.cancel()
    await asyncio.sleep(0)
    caller_finished_before_provider = caller.done()

    storage.thread_create_release.set()
    await asyncio.to_thread(storage.thread_create_finished.wait)
    with pytest.raises(asyncio.CancelledError):
        await caller
    await _assert_initialization_provider_id_is_durably_owned(
        auth_session_factory, storage, task.id
    )
    assert not caller_finished_before_provider
    assert not service._owned_settlement_tasks


@pytest.mark.asyncio
async def test_owned_cancellation_after_publish_commit_preserves_usable_winner(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    request = _same_digest_request("cancel-after-publish")
    publish_committed = asyncio.Event()
    publish_release = asyncio.Event()
    original = service._persist_initialized_upload

    async def blocked_after_commit(*args: object, **kwargs: object):
        result = await original(*args, **kwargs)
        publish_committed.set()
        await publish_release.wait()
        return result

    monkeypatch.setattr(service, "_persist_initialized_upload", blocked_after_commit)
    caller = asyncio.create_task(service.initiate(user.id, task.id, request))
    await publish_committed.wait()
    async with auth_session_factory() as session:
        asset = await session.scalar(select(Asset).where(Asset.task_id == task.id))
        assert asset is not None and asset.blob_id is not None
        blob = await session.get(StoredBlob, asset.blob_id)
        assert blob is not None and blob.provider_upload_id is not None
        published_provider_upload_id = blob.provider_upload_id

    owned = next(iter(service._owned_settlement_tasks))
    owned.cancel()
    publish_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller

    repeated = await service.initiate(user.id, task.id, request)
    assert repeated.provider_upload_id == published_provider_upload_id
    await service.sign_part(user.id, repeated.asset.id, 1)
    async with auth_session_factory() as session:
        cleanup_jobs = await session.scalar(
            select(func.count()).select_from(StorageCleanupJob)
        )
    assert storage.abort_call_count == 0
    assert storage.delete_call_count == 0
    assert cleanup_jobs == 0


@pytest.mark.asyncio
async def test_initiate_cancellation_before_provider_returns_waits_for_owned_settlement(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage(block_create=True)
    service = UploadService(auth_session_factory, storage)
    request = _same_digest_request("cancel-before-provider")

    caller = asyncio.create_task(service.initiate(user.id, task.id, request))
    await storage.create_started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()

    storage.create_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller

    await _assert_initialization_provider_id_is_durably_owned(
        auth_session_factory, storage, task.id
    )


@pytest.mark.asyncio
async def test_initiate_cancellation_after_provider_id_waits_for_cleanup_ownership(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    request = _same_digest_request("cancel-before-cleanup")
    record_started = asyncio.Event()
    record_release = asyncio.Event()
    original = service._record_initialization_cleanup

    async def blocked_record(*args: object, **kwargs: object) -> UUID:
        record_started.set()
        await record_release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_record_initialization_cleanup", blocked_record)
    caller = asyncio.create_task(service.initiate(user.id, task.id, request))
    await record_started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()

    record_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await _assert_initialization_provider_id_is_durably_owned(
        auth_session_factory, storage, task.id
    )


@pytest.mark.asyncio
async def test_initiate_cancellation_during_compensation_retry_waits_for_ownership(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    request = _same_digest_request("cancel-during-recovery")
    recovery_started = asyncio.Event()
    recovery_release = asyncio.Event()
    original_recovery = service._record_initialization_cleanup_recovery

    async def broken_record(*_args: object, **_kwargs: object) -> UUID:
        raise RuntimeError("cleanup transaction failed")

    async def blocked_recovery(*args: object, **kwargs: object) -> bool:
        recovery_started.set()
        await recovery_release.wait()
        return await original_recovery(*args, **kwargs)

    monkeypatch.setattr(service, "_record_initialization_cleanup", broken_record)
    monkeypatch.setattr(
        service, "_record_initialization_cleanup_recovery", blocked_recovery
    )
    caller = asyncio.create_task(service.initiate(user.id, task.id, request))
    await recovery_started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()

    recovery_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await _assert_initialization_provider_id_is_durably_owned(
        auth_session_factory, storage, task.id
    )


@pytest.mark.asyncio
async def test_repeated_initiate_cancellation_settles_without_owned_task_leak(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    request = _same_digest_request("repeated-cancel-before-cleanup")
    record_started = asyncio.Event()
    record_release = asyncio.Event()
    original = service._record_initialization_cleanup

    async def blocked_record(*args: object, **kwargs: object) -> UUID:
        record_started.set()
        await record_release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_record_initialization_cleanup", blocked_record)
    caller = asyncio.create_task(service.initiate(user.id, task.id, request))
    await record_started.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    assert not caller.done()

    record_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not service._owned_settlement_tasks
    await _assert_initialization_provider_id_is_durably_owned(
        auth_session_factory, storage, task.id
    )


@pytest.mark.asyncio
async def test_repeated_owned_initialization_cancellation_waits_for_exact_id_settlement(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage(fail_abort_times=1)
    service = UploadService(auth_session_factory, storage)
    request = _same_digest_request("owned-task-cancel")
    record_started = asyncio.Event()
    record_release = asyncio.Event()
    recovery_started = asyncio.Event()
    recovery_release = asyncio.Event()
    original_record = service._record_initialization_cleanup
    original_recovery = service._record_initialization_cleanup_recovery

    async def blocked_record(*args: object, **kwargs: object) -> UUID:
        record_started.set()
        await record_release.wait()
        return await original_record(*args, **kwargs)

    async def blocked_recovery(*args: object, **kwargs: object) -> bool:
        recovery_started.set()
        await recovery_release.wait()
        return await original_recovery(*args, **kwargs)

    monkeypatch.setattr(service, "_record_initialization_cleanup", blocked_record)
    monkeypatch.setattr(
        service, "_record_initialization_cleanup_recovery", blocked_recovery
    )
    caller = asyncio.create_task(service.initiate(user.id, task.id, request))
    await record_started.wait()
    owned = next(iter(service._owned_settlement_tasks))
    owned.cancel()
    await recovery_started.wait()
    owned.cancel()
    await asyncio.sleep(0)
    assert not caller.done()

    recovery_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not service._owned_settlement_tasks
    await _assert_initialization_provider_id_is_durably_owned(
        auth_session_factory, storage, task.id
    )


@pytest.mark.asyncio
async def test_new_unique_asset_at_task_capacity_is_rejected_without_count_change(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Allowing a new local asset at 10,000 would overfill a task."""
    user, task = await _task_with_accepted_assets(
        auth_session_factory, MAX_TASK_ACCEPTED_ASSETS
    )
    ready = StoredBlob(
        id=uuid4(),
        user_id=user.id,
        sha256="e" * 64,
        byte_size=8,
        object_key="private/ready-at-cap",
        is_ready=True,
    )
    async with auth_session_factory() as session:
        session.add(ready)
        await session.commit()
    service = UploadService(auth_session_factory, FakeObjectStorage())

    with pytest.raises(UploadConflict, match="task_asset_limit"):
        await service.initiate(
            user.id,
            task.id,
            AssetInitRequest(
                local_asset_id="new",
                filename="new.jpg",
                media_type="image/jpeg",
                byte_size=8,
                sha256="e" * 64,
            ),
        )

    async with auth_session_factory() as session:
        task_after = await session.get(ImportTask, task.id)
        count = await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.task_id == task.id)
        )
    assert count == MAX_TASK_ACCEPTED_ASSETS
    assert (
        task_after is not None and task_after.total_assets == MAX_TASK_ACCEPTED_ASSETS
    )


@pytest.mark.asyncio
async def test_same_task_different_local_id_duplicate_has_receipt_without_new_upload(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admitting the same task content twice consumes a slot and opens duplicate storage."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)

    canonical = await service.initiate(
        user.id, task.id, _same_digest_request("album:canonical")
    )
    duplicate = await service.initiate(
        user.id, task.id, _same_digest_request("album:duplicate")
    )

    async with auth_session_factory() as session:
        repository = AssetRepository(session)
        accepted = await repository.accepted_count(user.id, task.id)
        rows = list(
            (
                await session.scalars(
                    select(Asset)
                    .where(Asset.task_id == task.id)
                    .order_by(Asset.created_at, Asset.id)
                )
            ).all()
        )
        blobs = int(
            await session.scalar(
                select(func.count())
                .select_from(StoredBlob)
                .where(StoredBlob.user_id == user.id)
            )
            or 0
        )

    assert canonical.disposition == "accepted"
    assert duplicate.disposition == "duplicate"
    assert duplicate.already_uploaded is True
    assert duplicate.provider_upload_id is None
    assert duplicate.asset.id != canonical.asset.id
    assert duplicate.asset.canonical_asset_id == canonical.asset.id
    assert duplicate.asset.blob_id is None
    assert duplicate.asset.local_asset_id == "album:duplicate"
    assert duplicate.asset.status.value == "skipped_duplicate"
    assert accepted == 1
    assert len(rows) == 2
    assert blobs == 1
    assert storage.create_call_count == 1


@pytest.mark.asyncio
async def test_duplicate_receipt_retry_is_idempotent_and_rejects_payload_change(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A receipt must preserve its own source-id idempotency contract."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    request = _same_digest_request("album:duplicate-retry")

    canonical = await service.initiate(
        user.id, task.id, _same_digest_request("album:retry-canonical")
    )
    first = await service.initiate(user.id, task.id, request)
    retry = await service.initiate(user.id, task.id, request)
    with pytest.raises(UploadConflict, match="idempotency_payload_mismatch"):
        await service.initiate(
            user.id,
            task.id,
            request.model_copy(update={"filename": "changed.jpg"}),
        )

    assert first.disposition == retry.disposition == "duplicate"
    assert first.asset.id == retry.asset.id
    assert first.canonical_asset_id == retry.canonical_asset_id == canonical.asset.id
    assert storage.create_call_count == 1


@pytest.mark.asyncio
async def test_duplicate_at_task_capacity_succeeds_without_consuming_a_slot(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Checking capacity before content dedup rejects a harmless full-task receipt."""
    user, task = await _task_with_accepted_assets(
        auth_session_factory, MAX_TASK_ACCEPTED_ASSETS
    )
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)

    duplicate = await service.initiate(
        user.id,
        task.id,
        AssetInitRequest(
            local_asset_id="album:duplicate-at-capacity",
            filename="duplicate.jpg",
            media_type="image/jpeg",
            byte_size=8,
            sha256="0" * 64,
        ),
    )

    async with auth_session_factory() as session:
        repository = AssetRepository(session)
        accepted = await repository.accepted_count(user.id, task.id)
        task_after = await session.get(ImportTask, task.id)
        total_rows = int(
            await session.scalar(
                select(func.count()).select_from(Asset).where(Asset.task_id == task.id)
            )
            or 0
        )

    assert duplicate.disposition == "duplicate"
    assert duplicate.asset.canonical_asset_id is not None
    assert accepted == MAX_TASK_ACCEPTED_ASSETS
    assert task_after is not None
    assert task_after.total_assets == MAX_TASK_ACCEPTED_ASSETS
    assert total_rows == MAX_TASK_ACCEPTED_ASSETS + 1
    assert storage.create_call_count == 0


@pytest.mark.asyncio
async def test_duplicate_then_final_unique_moves_9999_to_exactly_10000(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Counting a duplicate would incorrectly consume the final unique-image slot."""
    user, task = await _task_with_accepted_assets(
        auth_session_factory, MAX_TASK_ACCEPTED_ASSETS - 1
    )
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)

    duplicate = await service.initiate(
        user.id,
        task.id,
        AssetInitRequest(
            local_asset_id="album:duplicate-before-final",
            filename="duplicate.jpg",
            media_type="image/jpeg",
            byte_size=8,
            sha256="0" * 64,
        ),
    )
    async with auth_session_factory() as session:
        after_duplicate = await AssetRepository(session).accepted_count(
            user.id, task.id
        )

    final_unique = await service.initiate(
        user.id, task.id, _same_digest_request("album:final-unique")
    )

    async with auth_session_factory() as session:
        repository = AssetRepository(session)
        accepted = await repository.accepted_count(user.id, task.id)
        task_after = await session.get(ImportTask, task.id)

    assert duplicate.disposition == "duplicate"
    assert after_duplicate == MAX_TASK_ACCEPTED_ASSETS - 1
    assert final_unique.disposition == "accepted"
    assert accepted == MAX_TASK_ACCEPTED_ASSETS
    assert task_after is not None
    assert task_after.total_assets == MAX_TASK_ACCEPTED_ASSETS
    assert storage.create_call_count == 1


@pytest.mark.asyncio
async def test_concurrent_same_task_digest_creates_one_canonical_and_one_receipt(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The task lock must elect one canonical before either request opens another upload."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage(create_delay_seconds=0.03)
    first = UploadService(auth_session_factory, storage)
    second = UploadService(auth_session_factory, storage)

    results = await asyncio.gather(
        first.initiate(user.id, task.id, _same_digest_request("album:race-one")),
        second.initiate(user.id, task.id, _same_digest_request("album:race-two")),
    )

    async with auth_session_factory() as session:
        rows = list(
            (await session.scalars(select(Asset).where(Asset.task_id == task.id))).all()
        )
        accepted = await AssetRepository(session).accepted_count(user.id, task.id)

    canonical = next(result for result in results if result.disposition == "accepted")
    duplicate = next(result for result in results if result.disposition == "duplicate")
    assert duplicate.asset.canonical_asset_id == canonical.asset.id
    assert sorted(asset.status.value for asset in rows) == [
        "skipped_duplicate",
        canonical.asset.status.value,
    ]
    assert accepted == 1
    assert storage.create_call_count == 1


@pytest.mark.asyncio
async def test_same_digest_in_different_tasks_counts_once_per_task_and_reuses_blob(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Task-local dedup must not collapse independently admitted task membership."""
    user = User(id=uuid4(), phone_e164=f"+8613{uuid4().int % 10**8:08d}")
    first_task = ImportTask(id=uuid4(), user_id=user.id, name="First task")
    second_task = ImportTask(id=uuid4(), user_id=user.id, name="Second task")
    ready = StoredBlob(
        id=uuid4(),
        user_id=user.id,
        sha256="7" * 64,
        byte_size=8,
        object_key="private/cross-task-ready",
        is_ready=True,
    )
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add_all([first_task, second_task, ready])
        await session.commit()
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    request = AssetInitRequest(
        local_asset_id="album:same-source-id-is-task-scoped",
        filename="same.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="7" * 64,
    )

    first_result = await service.initiate(user.id, first_task.id, request)
    second_result = await service.initiate(user.id, second_task.id, request)

    async with auth_session_factory() as session:
        repository = AssetRepository(session)
        first_count = await repository.accepted_count(user.id, first_task.id)
        second_count = await repository.accepted_count(user.id, second_task.id)

    assert first_result.disposition == second_result.disposition == "accepted"
    assert first_result.asset.blob_id == second_result.asset.blob_id == ready.id
    assert first_count == second_count == 1
    assert storage.create_call_count == 0


@pytest.mark.asyncio
async def test_post_admission_failure_keeps_the_final_reserved_slot(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dropping failed canonicals from accepted count would silently reopen capacity."""
    user, task = await _task_with_accepted_assets(
        auth_session_factory, MAX_TASK_ACCEPTED_ASSETS - 1
    )
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    admitted = await service.initiate(
        user.id, task.id, _same_digest_request("album:reserved-final")
    )

    await service.fail(user.id, admitted.asset.id, "client_failed", "client failed")
    with pytest.raises(UploadConflict, match="task_asset_limit"):
        await service.initiate(
            user.id,
            task.id,
            AssetInitRequest(
                local_asset_id="album:after-failure",
                filename="after.jpg",
                media_type="image/jpeg",
                byte_size=8,
                sha256="e" * 64,
            ),
        )

    async with auth_session_factory() as session:
        repository = AssetRepository(session)
        accepted = await repository.accepted_count(user.id, task.id)
        failed = await session.get(Asset, admitted.asset.id)
        task_after = await session.get(ImportTask, task.id)

    assert failed is not None and failed.status == AssetStatus.FAILED
    assert accepted == MAX_TASK_ACCEPTED_ASSETS
    assert task_after is not None
    assert task_after.total_assets == MAX_TASK_ACCEPTED_ASSETS
    assert task_after.failed_assets == 1


@pytest.mark.asyncio
async def test_new_unique_asset_uses_the_final_task_slot(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rejecting a unique local asset while one slot remains loses valid input."""
    user, task = await _task_with_accepted_assets(
        auth_session_factory, MAX_TASK_ACCEPTED_ASSETS - 1
    )
    service = UploadService(auth_session_factory, FakeObjectStorage())

    result = await service.initiate(
        user.id,
        task.id,
        _same_digest_request("final-slot"),
    )

    async with auth_session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.task_id == task.id)
        )
    assert result.asset.local_asset_id == "final-slot"
    assert count == MAX_TASK_ACCEPTED_ASSETS


@pytest.mark.asyncio
async def test_existing_local_asset_at_task_capacity_returns_its_idempotent_result(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Checking capacity before local-id idempotency breaks upload retry recovery."""
    user, task = await _task_with_accepted_assets(
        auth_session_factory, MAX_TASK_ACCEPTED_ASSETS
    )
    service = UploadService(auth_session_factory, FakeObjectStorage())

    result = await service.initiate(
        user.id,
        task.id,
        AssetInitRequest(
            local_asset_id="prior:0",
            filename="prior.jpg",
            media_type="image/jpeg",
            byte_size=8,
            sha256=f"{0:064x}",
        ),
    )

    assert result.asset.local_asset_id == "prior:0"
    assert result.already_uploaded is True


@pytest.mark.asyncio
async def test_concurrent_last_slot_allows_exactly_one_initiation(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A task-row lock prevents two initiators from both consuming the final slot."""
    user, task = await _task_with_accepted_assets(
        auth_session_factory, MAX_TASK_ACCEPTED_ASSETS - 1
    )
    storage = FakeObjectStorage()
    first_service = UploadService(auth_session_factory, storage)
    second_service = UploadService(auth_session_factory, storage)
    first_request = AssetInitRequest(
        local_asset_id="last-slot:first",
        filename="first.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="a" * 64,
    )
    second_request = AssetInitRequest(
        local_asset_id="last-slot:second",
        filename="second.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="b" * 64,
    )

    results = await asyncio.gather(
        first_service.initiate(user.id, task.id, first_request),
        second_service.initiate(user.id, task.id, second_request),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, UploadConflict)]
    assert len(successes) == 1
    assert len(failures) == 1 and str(failures[0]) == "task_asset_limit"
    async with auth_session_factory() as session:
        task_after = await session.get(ImportTask, task.id)
        count = await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.task_id == task.id)
        )
    assert count == MAX_TASK_ACCEPTED_ASSETS
    assert (
        task_after is not None and task_after.total_assets == MAX_TASK_ACCEPTED_ASSETS
    )


@pytest.mark.asyncio
async def test_live_initialization_claim_is_not_stolen_or_duplicated(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second service must observe a live PENDING claim instead of opening another multipart."""
    storage = FakeObjectStorage(create_delay_seconds=0.08)
    first = UploadService(auth_session_factory, storage, initialization_lease_seconds=1)
    second = UploadService(
        auth_session_factory, storage, initialization_lease_seconds=1
    )
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    request = _same_digest_request("same-local")

    in_progress = asyncio.create_task(first.initiate(user.id, task.id, request))
    await asyncio.sleep(0.02)
    observed = await second.initiate(user.id, task.id, request)
    initiated = await in_progress

    assert storage.create_call_count == 1
    assert observed.asset.status == AssetStatus.PENDING
    assert initiated.asset.status == AssetStatus.UPLOADING
    assert initiated.provider_upload_id is not None


@pytest.mark.asyncio
async def test_expired_initialization_claim_reclaims_with_next_generation(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A dead initializer must be fenced and retryable, not permanently PENDING."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    asset_id, blob_id = uuid4(), uuid4()
    blob = StoredBlob(
        id=blob_id,
        user_id=user.id,
        sha256="d" * 64,
        byte_size=8,
        object_key="private/stale-init",
        initialization_claim_id="dead",
        initialization_claimed_at=datetime(2000, 1, 1, tzinfo=UTC),
        initialization_generation=4,
    )
    asset = Asset(
        id=asset_id,
        user_id=user.id,
        task_id=task.id,
        blob_id=blob_id,
        local_asset_id="stale",
        filename="IMG.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="d" * 64,
        status=AssetStatus.PENDING,
    )
    async with auth_session_factory() as session:
        session.add_all([blob, asset])
        await session.commit()
    storage = FakeObjectStorage()
    service = UploadService(
        auth_session_factory, storage, initialization_lease_seconds=0.01
    )

    result = await service.initiate(user.id, task.id, _same_digest_request("stale"))
    async with auth_session_factory() as session:
        refreshed = await session.get(StoredBlob, blob_id)

    assert result.asset.status == AssetStatus.UPLOADING
    assert storage.create_call_count == 1
    assert refreshed is not None and refreshed.initialization_generation == 5


@pytest.mark.asyncio
async def test_old_initializer_failure_cannot_fail_newer_claim(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An old create error cannot overwrite the state claimed by a reclaimer."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    asset_id, blob_id = uuid4(), uuid4()
    blob = StoredBlob(
        id=blob_id,
        user_id=user.id,
        sha256="d" * 64,
        byte_size=8,
        object_key="private/fenced-init",
        initialization_claim_id="old",
        initialization_claimed_at=datetime(2000, 1, 1, tzinfo=UTC),
        initialization_generation=1,
    )
    asset = Asset(
        id=asset_id,
        user_id=user.id,
        task_id=task.id,
        blob_id=blob_id,
        local_asset_id="fenced",
        filename="IMG.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="d" * 64,
        status=AssetStatus.PENDING,
    )
    async with auth_session_factory() as session:
        session.add_all([blob, asset])
        await session.commit()
    service = UploadService(
        auth_session_factory, FakeObjectStorage(), initialization_lease_seconds=0.01
    )
    claimed = await service._claim_initialization(user.id, asset_id)
    assert claimed is not None
    token, generation = claimed
    await service._record_initialization_failure(user.id, asset_id, "old", 1)
    async with auth_session_factory() as session:
        asset_after = await session.get(Asset, asset_id)
        blob_after = await session.get(StoredBlob, blob_id)

    assert asset_after is not None and asset_after.status == AssetStatus.PENDING
    assert blob_after is not None
    assert (
        blob_after.initialization_claim_id,
        blob_after.initialization_generation,
    ) == (token, generation)


@pytest.mark.asyncio
async def test_provider_create_persistence_failure_leaves_abort_job_and_reclaimable_pending(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after create is compensated durably before the row can be retried."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(
        auth_session_factory, storage, initialization_lease_seconds=0.01
    )

    async def fail_persist(*_args, **_kwargs):
        raise RuntimeError("injected persistence crash")

    monkeypatch.setattr(service, "_persist_initialized_upload", fail_persist)
    with pytest.raises(RuntimeError, match="injected persistence crash"):
        await service.initiate(user.id, task.id, _same_digest_request("crash"))
    async with auth_session_factory() as session:
        asset = await session.scalar(select(Asset).where(Asset.task_id == task.id))
        job = await session.scalar(select(StorageCleanupJob))
        assert asset is not None and job is not None
        blob = await session.get(StoredBlob, asset.blob_id)
        assert blob is not None
        blob.initialization_claimed_at = datetime(2000, 1, 1, tzinfo=UTC)
        await session.commit()

    retry = await UploadService(
        auth_session_factory, storage, initialization_lease_seconds=0.01
    ).initiate(user.id, task.id, _same_digest_request("crash"))
    assert retry.asset.status == AssetStatus.UPLOADING
    assert storage.create_call_count == 2
    await service.process_cleanup_jobs(user.id)
    assert storage.abort_call_count >= 1


@pytest.mark.asyncio
async def test_cleanup_transient_retry_retains_provider_id_then_absence_succeeds(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Cleanup errors do not change the durable API result or discard retry identifiers."""
    storage = FakeObjectStorage(fail_abort_times=1)
    storage.seed_upload("private/active")
    service = UploadService(auth_session_factory, storage)
    async with auth_session_factory() as session:
        session.add(
            StorageCleanupJob(
                user_id=active_upload.user_id,
                blob_id=active_upload.blob_id,
                operation="abort_multipart",
                object_key="private/active",
                provider_upload_id="upload-1",
            )
        )
        await session.commit()

    await service.process_cleanup_jobs(active_upload.user_id)
    async with auth_session_factory() as session:
        job = await session.scalar(select(StorageCleanupJob))
    assert (
        job is not None and job.attempts == 1 and job.provider_upload_id == "upload-1"
    )
    await service.process_cleanup_jobs(active_upload.user_id)
    await service.process_cleanup_jobs(
        active_upload.user_id
    )  # already absent is success
    async with auth_session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(StorageCleanupJob))
            == 0
        )
    assert storage.abort_call_count == 2


@pytest.mark.asyncio
async def test_size_mismatch_persists_failure_and_retryable_delete_job(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Size mismatch must commit FAILED and cleanup work even when deletion is transient."""
    storage = FakeObjectStorage(completed_size=9, fail_delete_times=1)
    storage.seed_upload("private/active")
    service = UploadService(auth_session_factory, storage)

    with pytest.raises(Exception, match="object_size_mismatch"):
        await service.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    async with auth_session_factory() as session:
        failed = await session.get(Asset, active_upload.id)
        job = await session.scalar(select(StorageCleanupJob))
    assert failed is not None and failed.status == AssetStatus.FAILED
    assert job is not None and job.operation == "delete_object" and job.attempts == 1
    await service.process_cleanup_jobs(active_upload.user_id)
    async with auth_session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(StorageCleanupJob))
            == 0
        )


@pytest.mark.asyncio
async def test_cleanup_job_is_durably_claimed_before_provider_abort(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """A second worker must see an owned cleanup job while the first is in provider I/O."""
    storage = FakeObjectStorage(block_abort=True)
    storage.seed_upload("private/active")
    service = UploadService(auth_session_factory, storage)
    async with auth_session_factory() as session:
        session.add(
            StorageCleanupJob(
                user_id=active_upload.user_id,
                blob_id=active_upload.blob_id,
                operation="abort_multipart",
                object_key="private/active",
                provider_upload_id="upload-1",
            )
        )
        await session.commit()
    worker = asyncio.create_task(service.process_cleanup_jobs(active_upload.user_id))
    await storage.abort_started.wait()
    async with auth_session_factory() as session:
        job = await session.scalar(select(StorageCleanupJob))

    assert job is not None and job.status == "processing" and job.claim_id is not None
    storage.abort_release.set()
    await worker


@pytest.mark.asyncio
async def test_abort_not_found_then_transient_delete_returns_job_to_retryable_pending(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """The abort→head→delete recovery chain is one durable retry unit."""
    storage = FakeObjectStorage()
    storage._heads["private/active"] = ObjectHead(8, "etag", None)
    storage.inject_fault("abort", StorageNotFound, times=2)
    storage.inject_fault("delete", StorageTransient)
    service = UploadService(auth_session_factory, storage)
    async with auth_session_factory() as session:
        session.add(
            StorageCleanupJob(
                user_id=active_upload.user_id,
                blob_id=active_upload.blob_id,
                operation="abort_multipart",
                object_key="private/active",
                provider_upload_id="upload-1",
            )
        )
        await session.commit()

    await service.process_cleanup_jobs(active_upload.user_id)
    async with auth_session_factory() as session:
        job = await session.scalar(select(StorageCleanupJob))
    assert job is not None and job.status == "pending" and job.attempts == 1

    await service.process_cleanup_jobs(active_upload.user_id)
    async with auth_session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(StorageCleanupJob))
            == 0
        )
    with pytest.raises(StorageNotFound):
        await storage.head_object("private/active")


@pytest.mark.asyncio
async def test_heartbeat_failure_is_observed_and_prevents_finalization(
    auth_session_factory: async_sessionmaker[AsyncSession],
    active_upload: Asset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unobserved renewal exception must not let a worker finalize uncertain ownership."""
    storage = FakeObjectStorage(complete_delay_seconds=0.02)
    storage.seed_upload("private/active")
    service = UploadService(auth_session_factory, storage)

    async def broken_heartbeat(*_args: object) -> None:
        raise RuntimeError("renewal database failure")

    monkeypatch.setattr(service, "_renew_completion_claim", broken_heartbeat)
    with pytest.raises(Exception, match="upload_claim_uncertain"):
        await service.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    async with auth_session_factory() as session:
        asset = await session.get(Asset, active_upload.id)
    assert asset is not None and asset.status == AssetStatus.UPLOADING
    assert storage.complete_attempt_count == 1


@pytest.mark.asyncio
async def test_failed_heartbeat_cannot_overlap_a_second_completion_worker(
    auth_session_factory: async_sessionmaker[AsyncSession],
    active_upload: Asset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed lease must not let a reclaimer overlap the still-live provider call."""
    storage = FakeObjectStorage(block_complete=True)
    storage.seed_upload("private/active")
    first = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)
    second = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)

    async def broken_heartbeat(*_args: object) -> None:
        raise RuntimeError("renewal database failure")

    monkeypatch.setattr(first, "_renew_completion_claim", broken_heartbeat)
    first_task = asyncio.create_task(
        first.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await storage.complete_started.wait()
    await asyncio.sleep(0.02)
    second_task = asyncio.create_task(
        second.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    try:
        await asyncio.sleep(0.02)
        assert storage.max_concurrent_complete_calls == 1
    finally:
        storage.complete_release.set()
    with pytest.raises(Exception, match="upload_claim_uncertain"):
        await first_task
    winner = await second_task

    assert winner.asset.status == AssetStatus.UPLOADED
    assert storage.complete_attempt_count == 1
    assert storage.max_concurrent_complete_calls == 1
    assert storage.delete_calls_by_key.get("private/active", 0) == 0
    assert await storage.head_object("private/active") == ObjectHead(
        8, "fake-etag", None
    )


@pytest.mark.asyncio
async def test_cancelled_completion_settles_provider_before_second_worker_can_recover(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Cancelling the request cannot abandon a live provider completion thread/call."""
    storage = FakeObjectStorage(block_complete=True)
    storage.seed_upload("private/active")
    first = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)
    second = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)
    first_task = asyncio.create_task(
        first.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await storage.complete_started.wait()
    first_task.cancel()
    second_task = asyncio.create_task(
        second.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await asyncio.sleep(0.02)
    assert storage.max_concurrent_complete_calls == 1
    storage.complete_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    completed = await second_task

    assert completed.asset.status == AssetStatus.UPLOADED
    assert storage.complete_attempt_count == 1
    assert storage.max_concurrent_complete_calls == 1
    assert storage.delete_calls_by_key.get("private/active", 0) == 0
    assert await storage.head_object("private/active") == ObjectHead(
        8, "fake-etag", None
    )


@pytest.mark.asyncio
async def test_many_completion_waiters_release_connections_between_try_lock_attempts(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Contenders back off outside sessions; one provider attempt serves every waiter."""
    storage = FakeObjectStorage(block_complete=True)
    storage.seed_upload("private/active")
    services = [
        UploadService(auth_session_factory, storage, provider_lock_attempts=80)
        for _ in range(6)
    ]
    owner = asyncio.create_task(
        services[0].complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await storage.complete_started.wait()
    waiters = [
        asyncio.create_task(
            service.complete(
                active_upload.user_id,
                active_upload.id,
                [CompletedPart(part_number=1, etag="etag")],
            )
        )
        for service in services[1:]
    ]
    await asyncio.sleep(0.03)
    storage.complete_release.set()
    results = await asyncio.gather(owner, *waiters)

    assert all(result.asset.status == AssetStatus.UPLOADED for result in results)
    assert storage.complete_attempt_count == 1
    assert storage.max_concurrent_complete_calls == 1


@pytest.mark.asyncio
async def test_sealed_task_rejects_sign_before_storage_but_uploaded_retry_is_idempotent(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Terminal tasks cannot emit new capabilities, yet completed retries remain readable."""
    storage = FakeObjectStorage()
    storage.seed_upload("private/active")
    service = UploadService(auth_session_factory, storage)
    await service.complete(
        active_upload.user_id,
        active_upload.id,
        [CompletedPart(part_number=1, etag="etag")],
    )
    async with auth_session_factory() as session:
        task = await session.get(ImportTask, active_upload.task_id)
        assert task is not None
        task.status = TaskStatus.CANCELLED
        await session.commit()

    repeated = await service.complete(
        active_upload.user_id,
        active_upload.id,
        [CompletedPart(part_number=1, etag="etag")],
    )
    with pytest.raises(Exception, match="task_sealed"):
        await service.sign_part(active_upload.user_id, active_upload.id, 1)

    assert repeated.asset.status == AssetStatus.UPLOADED
    assert storage.complete_call_count == 1
    assert storage.sign_call_count == 0


@pytest.mark.asyncio
async def test_cancel_during_blocked_completion_prevents_finalization_and_cleans_upload(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """A terminal transition between claim and provider work must not finalize afterward."""
    storage = FakeObjectStorage(block_complete=True)
    storage.seed_upload("private/active")
    upload = UploadService(auth_session_factory, storage)
    completion = asyncio.create_task(
        upload.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await storage.complete_started.wait()
    cancelled = await TaskService(auth_session_factory).cancel(
        active_upload.user_id, active_upload.task_id
    )
    storage.complete_release.set()
    with pytest.raises(Exception, match="upload_not_active|task_sealed"):
        await completion
    await upload.process_cleanup_jobs(active_upload.user_id)

    async with auth_session_factory() as session:
        asset = await session.get(Asset, active_upload.id)
        task = await session.get(ImportTask, active_upload.task_id)
        jobs = await session.scalar(select(func.count()).select_from(StorageCleanupJob))
    assert cancelled.status == TaskStatus.CANCELLED
    assert asset is not None and asset.status == AssetStatus.FAILED
    assert task is not None and task.failed_assets == 1
    assert jobs == 0
    with pytest.raises(StorageNotFound):
        await storage.head_object("private/active")


@pytest.mark.asyncio
async def test_cancel_best_effort_drains_its_durable_cleanup_jobs(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Cancelling through the task service must not require a separate manual drain."""
    storage = FakeObjectStorage()
    storage.seed_upload("private/active")
    upload = UploadService(auth_session_factory, storage)
    tasks = TaskService(
        auth_session_factory, cleanup_processor=upload.process_cleanup_jobs
    )

    cancelled = await tasks.cancel(active_upload.user_id, active_upload.task_id)

    async with auth_session_factory() as session:
        jobs = await session.scalar(select(func.count()).select_from(StorageCleanupJob))
    assert cancelled.status == TaskStatus.CANCELLED
    assert jobs == 0
    assert storage.abort_call_count == 1


@pytest.mark.asyncio
async def test_cleanup_record_failure_aborts_then_leaves_a_retryable_pending_initializer(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed first compensation write may not orphan the provider upload."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    original = service._record_initialization_cleanup

    async def broken_record(*_args: object) -> object:
        raise RuntimeError("cleanup transaction failed")

    monkeypatch.setattr(service, "_record_initialization_cleanup", broken_record)
    request = _same_digest_request("recoverable-init")
    with pytest.raises(UploadConflict, match="upload_initialization_recovery_required"):
        await service.initiate(user.id, task.id, request)
    monkeypatch.setattr(service, "_record_initialization_cleanup", original)

    async with auth_session_factory() as session:
        asset = await session.scalar(select(Asset).where(Asset.task_id == task.id))
        assert asset is not None
        blob = await session.get(StoredBlob, asset.blob_id)
        jobs = await session.scalar(select(func.count()).select_from(StorageCleanupJob))
    assert asset.status == AssetStatus.PENDING
    assert (
        blob is not None
        and blob.provider_upload_id is None
        and blob.cleanup_pending is False
    )
    assert jobs == 0
    assert storage.abort_call_count == 1

    retried = await service.initiate(user.id, task.id, request)
    assert retried.asset.id == asset.id
    assert retried.provider_upload_id is not None


@pytest.mark.asyncio
async def test_cleanup_record_failure_with_transient_abort_persists_exact_provider_recovery(
    auth_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If abort is unavailable, the exact provider ID is durable until a retry settles it."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage(fail_abort_times=2)
    service = UploadService(auth_session_factory, storage)
    original_recovery = service._record_initialization_cleanup_recovery
    recovery_calls = 0

    async def broken_record(*_args: object) -> object:
        raise RuntimeError("cleanup transaction failed")

    async def first_recovery_write_fails(*args: object, **kwargs: object) -> bool:
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 1:
            raise RuntimeError("fallback cleanup transaction failed")
        return await original_recovery(*args, **kwargs)

    monkeypatch.setattr(service, "_record_initialization_cleanup", broken_record)
    monkeypatch.setattr(
        service, "_record_initialization_cleanup_recovery", first_recovery_write_fails
    )
    request = _same_digest_request("durable-provider-id")
    with pytest.raises(Exception, match="upload_initialization_recovery_required"):
        await service.initiate(user.id, task.id, request)

    async with auth_session_factory() as session:
        asset = await session.scalar(select(Asset).where(Asset.task_id == task.id))
        assert asset is not None
        blob = await session.get(StoredBlob, asset.blob_id)
        jobs = list((await session.scalars(select(StorageCleanupJob))).all())
    assert (
        blob is not None
        and blob.provider_upload_id is not None
        and blob.cleanup_pending is True
    )
    assert len(jobs) == 1 and jobs[0].operation == "abort_multipart"
    assert recovery_calls >= 2

    await service.process_cleanup_jobs(user.id)
    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, asset.blob_id)
        jobs = await session.scalar(select(func.count()).select_from(StorageCleanupJob))
    assert (
        blob is not None
        and blob.provider_upload_id is None
        and blob.cleanup_pending is False
    )
    assert jobs == 0


@pytest.mark.asyncio
async def test_stale_initializer_compensation_persists_orphan_without_overwriting_new_upload(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """A fenced-out initializer records its exact orphan job but cannot touch the winner."""
    service = UploadService(auth_session_factory, FakeObjectStorage())
    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, active_upload.blob_id)
        assert blob is not None
        blob.provider_upload_id = "new-winner-upload"
        blob.initialization_claim_id = "new-winner-claim"
        blob.initialization_generation = 9
        await session.commit()

    persisted = await service._record_initialization_cleanup_recovery(
        active_upload.user_id,
        active_upload.id,
        "stale-claim",
        8,
        "private/active",
        "orphan-upload",
        retain_provider_upload=True,
    )

    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, active_upload.blob_id)
        job = await session.scalar(
            select(StorageCleanupJob).where(
                StorageCleanupJob.provider_upload_id == "orphan-upload"
            )
        )
    assert persisted is True
    assert blob is not None and blob.provider_upload_id == "new-winner-upload"
    assert job is not None and job.object_key == "private/active"


@pytest.mark.asyncio
async def test_fail_wins_against_blocked_completion_without_deadlock_or_orphan(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Both paths lock task then asset/blob; one terminal outcome wins cleanly."""
    storage = FakeObjectStorage(block_complete=True)
    storage.seed_upload("private/active")
    service = UploadService(auth_session_factory, storage)
    completion = asyncio.create_task(
        service.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await storage.complete_started.wait()
    failed = await service.fail(
        active_upload.user_id, active_upload.id, "client_failed", "client failed"
    )
    storage.complete_release.set()
    with pytest.raises(Exception, match="upload_not_active"):
        await completion
    await service.process_cleanup_jobs(active_upload.user_id)

    assert failed.status == AssetStatus.FAILED
    with pytest.raises(StorageNotFound):
        await storage.head_object("private/active")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("filename", "changed.jpg"),
        ("media_type", "image/png"),
        ("byte_size", 9),
        ("sha256", "e" * 64),
        ("captured_at", datetime(2026, 8, 14, tzinfo=UTC)),
    ],
)
async def test_retry_and_pending_reclaim_reject_each_immutable_payload_change(
    auth_session_factory: async_sessionmaker[AsyncSession],
    field: str,
    replacement: object,
) -> None:
    """Retry identity includes every immutable client-provided metadata field."""
    user, task = await _task_with_accepted_assets(auth_session_factory, 0)
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)
    original = AssetInitRequest(
        local_asset_id="immutable",
        filename="IMG.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="d" * 64,
        captured_at=None,
    )
    initiated = await service.initiate(user.id, task.id, original)
    changed = original.model_copy(update={field: replacement})
    await service.fail(user.id, initiated.asset.id, "failed", "failed")
    with pytest.raises(Exception, match="idempotency_payload_mismatch"):
        await service.initiate(
            user.id, task.id, changed.model_copy(update={"retry": True})
        )

    async with auth_session_factory() as session:
        asset = await session.get(Asset, initiated.asset.id)
        blob = await session.get(StoredBlob, initiated.asset.blob_id)
        assert asset is not None and blob is not None
        asset.status = AssetStatus.PENDING
        blob.provider_upload_id = None
        blob.initialization_claim_id = None
        blob.initialization_claimed_at = None
        await session.commit()
    with pytest.raises(Exception, match="idempotency_payload_mismatch"):
        await service.initiate(user.id, task.id, changed)

    accepted = await service.initiate(user.id, task.id, original)
    assert accepted.asset.status == AssetStatus.UPLOADING


@pytest.mark.asyncio
async def test_heartbeat_uncertainty_never_deletes_the_winning_object(
    auth_session_factory: async_sessionmaker[AsyncSession],
    active_upload: Asset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loser that lost its heartbeat must reconcile, never delete the winner."""
    storage = FakeObjectStorage(block_complete=True)
    storage.seed_upload("private/active")
    first = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)
    second = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)

    async def broken_heartbeat(*_args: object) -> None:
        raise RuntimeError("renewal failed")

    monkeypatch.setattr(first, "_renew_completion_claim", broken_heartbeat)
    first_task = asyncio.create_task(
        first.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await storage.complete_started.wait()
    second_task = asyncio.create_task(
        second.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    storage.complete_release.set()
    with pytest.raises(Exception, match="upload_claim_uncertain"):
        await first_task
    await second_task

    assert storage.delete_calls_by_key.get("private/active", 0) == 0
    assert await storage.head_object("private/active") == ObjectHead(
        8, "fake-etag", None
    )


@pytest.mark.asyncio
async def test_repeated_request_cancellation_settles_once_without_winner_deletion(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Multiple cancellation signals cannot orphan the provider operation task."""
    storage = FakeObjectStorage(block_complete=True)
    storage.seed_upload("private/active")
    first = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)
    second = UploadService(auth_session_factory, storage, completion_lease_seconds=0.01)
    owner = asyncio.create_task(
        first.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    await storage.complete_started.wait()
    owner.cancel()
    owner.cancel()
    waiter = asyncio.create_task(
        second.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    )
    storage.complete_release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await waiter

    assert storage.complete_attempt_count == 1
    assert storage.max_concurrent_complete_calls == 1
    assert storage.delete_calls_by_key.get("private/active", 0) == 0
    assert await storage.head_object("private/active") == ObjectHead(
        8, "fake-etag", None
    )


@pytest.mark.asyncio
async def test_await_settlement_propagates_cancelled_child_without_spinning() -> None:
    """Treating a cancelled child as caller cancellation spins the waiter forever."""

    async def cancelled_child() -> None:
        raise asyncio.CancelledError

    child = asyncio.create_task(cancelled_child())
    waiter = asyncio.create_task(UploadService._await_settlement(child))

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=0.2)
    assert child.cancelled()
    assert waiter.done()


@pytest.mark.asyncio
async def test_await_settlement_observes_repeated_caller_cancellation() -> None:
    """A repeated caller cancellation must not cancel the owned child task."""
    release = asyncio.Event()
    child_started = asyncio.Event()

    async def child_operation() -> str:
        child_started.set()
        await release.wait()
        return "settled"

    child = asyncio.create_task(child_operation())
    waiter = asyncio.create_task(UploadService._await_settlement(child))
    await child_started.wait()
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    waiter.cancel()
    release.set()

    result, cancelled = await waiter
    assert result == "settled"
    assert cancelled is True
    assert child.done() and not child.cancelled()


@pytest.mark.asyncio
async def test_cleanup_operation_settlement_has_exact_provider_calls(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Delete once; abort success never deletes; abort-not-found probes then deletes once."""
    storage = FakeObjectStorage()
    service = UploadService(auth_session_factory, storage)

    await service._execute_cleanup_operation("delete_object", "delete-once", None)
    storage.seed_upload("abort-only")
    await service._execute_cleanup_operation(
        "abort_multipart", "abort-only", "upload-1"
    )
    storage._heads["abort-gone"] = ObjectHead(8, "etag", None)
    storage.inject_fault("abort", StorageNotFound)
    await service._execute_cleanup_operation(
        "abort_multipart", "abort-gone", "upload-missing"
    )

    assert storage.delete_calls_by_key["delete-once"] == 1
    assert storage.delete_calls_by_key.get("abort-only", 0) == 0
    assert storage.operation_log[-2:] == [
        ("head", "abort-gone"),
        ("delete", "abort-gone"),
    ]
    assert storage.delete_calls_by_key["abort-gone"] == 1


@pytest.mark.asyncio
async def test_cleanup_permanent_failure_deadletters_with_attempt_and_safe_code(
    auth_session_factory: async_sessionmaker[AsyncSession], active_upload: Asset
) -> None:
    """Permanent provider errors are accountable and never retain a processing claim."""
    storage = FakeObjectStorage()
    storage.inject_fault("delete", StoragePermanent)
    service = UploadService(auth_session_factory, storage)
    async with auth_session_factory() as session:
        session.add(
            StorageCleanupJob(
                user_id=active_upload.user_id,
                blob_id=active_upload.blob_id,
                operation="delete_object",
                object_key="private/active",
            )
        )
        await session.commit()
    await service.process_cleanup_jobs(active_upload.user_id)
    async with auth_session_factory() as session:
        job = await session.scalar(select(StorageCleanupJob))
    assert job is not None
    assert (
        job.status,
        job.attempts,
        job.terminal_code,
        job.claim_id,
        job.claimed_at,
    ) == ("dead", 1, "storage_permanent", None, None)


@pytest.mark.asyncio
async def test_size_mismatch_keeps_stable_business_error_when_immediate_drain_fails(
    auth_session_factory: async_sessionmaker[AsyncSession],
    active_upload: Asset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit cleanup outage must not replace object_size_mismatch."""
    storage = FakeObjectStorage(completed_size=9)
    storage.seed_upload("private/active")
    service = UploadService(auth_session_factory, storage)

    async def broken_drain(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cleanup db unavailable")

    monkeypatch.setattr(service, "process_cleanup_jobs", broken_drain)
    with pytest.raises(Exception, match="object_size_mismatch"):
        await service.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    async with auth_session_factory() as session:
        asset = await session.get(Asset, active_upload.id)
        job = await session.scalar(select(StorageCleanupJob))
    assert asset is not None and asset.status == AssetStatus.FAILED
    assert job is not None and job.operation == "delete_object"


@pytest.mark.asyncio
async def test_recovered_wrong_size_persists_failure_and_delete_before_stable_error(
    auth_session_factory: async_sessionmaker[AsyncSession],
    active_upload: Asset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Head-based recovery takes the same failure-atomic wrong-size path."""
    storage = FakeObjectStorage(completed_size=9)
    storage.seed_upload("private/active")
    await storage.complete_multipart(
        "private/active", "upload-1", [MultipartPart(1, "etag")]
    )
    service = UploadService(auth_session_factory, storage)
    async with auth_session_factory() as session:
        blob = await session.get(StoredBlob, active_upload.blob_id)
        assert blob is not None
        blob.completion_claim_id = "crashed"
        blob.completion_generation = 4
        blob.completion_claimed_at = datetime(2000, 1, 1, tzinfo=UTC)
        await session.commit()

    async def broken_drain(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cleanup db unavailable")

    monkeypatch.setattr(service, "process_cleanup_jobs", broken_drain)
    with pytest.raises(Exception, match="object_size_mismatch"):
        await service.complete(
            active_upload.user_id,
            active_upload.id,
            [CompletedPart(part_number=1, etag="etag")],
        )
    async with auth_session_factory() as session:
        asset = await session.get(Asset, active_upload.id)
        job = await session.scalar(select(StorageCleanupJob))
    assert asset is not None and asset.status == AssetStatus.FAILED
    assert job is not None and job.operation == "delete_object"
