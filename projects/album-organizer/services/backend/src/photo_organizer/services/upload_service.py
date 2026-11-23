import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.domain.limits import MAX_IMAGE_BYTES
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.cleanup_job import StorageCleanupJob
from photo_organizer.models.task import ImportTask
from photo_organizer.models.upload_binding import ControlledUploadBinding
from photo_organizer.repositories.asset_repository import AssetRepository
from photo_organizer.repositories.task_repository import TaskRepository
from photo_organizer.repositories.unit_of_work import UnitOfWork
from photo_organizer.schemas.assets import (
    AssetInitRequest,
    CompletedPart,
    UnsupportedAssetRequest,
)
from photo_organizer.schemas.controlled_upload_candidate import (
    ControlledUploadCandidate,
)
from photo_organizer.services.task_capacity_coordinator import (
    TaskCapacityCoordinator,
    TaskCapacityExceeded,
)
from photo_organizer.storage.object_storage import (
    MultipartPart,
    ObjectHead,
    ObjectStorage,
    StorageNotFound,
    StoragePermanent,
    StorageTransient,
)

PART_URL_TTL_SECONDS = 15 * 60
COMPLETION_LEASE_SECONDS = 1
INITIALIZATION_LEASE_SECONDS = 30
PROVIDER_LOCK_ATTEMPTS = 20
PROVIDER_LOCK_BACKOFF_SECONDS = 0.01
T = TypeVar("T")


class UploadError(Exception):
    """Expected upload boundary failures with stable public error codes."""


class UploadNotFound(UploadError):
    pass


class UploadConflict(UploadError):
    pass


class InvalidParts(UploadError):
    pass


@dataclass(frozen=True)
class InitiateResult:
    asset: Asset
    already_uploaded: bool
    provider_upload_id: str | None
    completed_parts: list[int]
    disposition: Literal["accepted", "duplicate"] = "accepted"
    canonical_asset_id: UUID | None = None


@dataclass(frozen=True)
class CompleteResult:
    asset: Asset


@dataclass(frozen=True)
class SignedPart:
    url: str
    expires_seconds: int = PART_URL_TTL_SECONDS


class UploadService:
    """Coordinates tenant-private, resumable direct multipart uploads."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        *,
        completion_lease_seconds: float = COMPLETION_LEASE_SECONDS,
        initialization_lease_seconds: float = INITIALIZATION_LEASE_SECONDS,
        provider_lock_attempts: int = PROVIDER_LOCK_ATTEMPTS,
    ) -> None:
        self._session_factory = session_factory
        self.storage = storage
        self._completion_lease_seconds = completion_lease_seconds
        self._initialization_lease_seconds = initialization_lease_seconds
        self._provider_lock_attempts = provider_lock_attempts
        self._owned_settlement_tasks: set[asyncio.Task[object]] = set()

    async def initiate(
        self, user_id: UUID, task_id: UUID, request: AssetInitRequest
    ) -> InitiateResult:
        """Create durable local state before the external multipart operation."""
        if request.media_type not in {"image/heic", "image/jpeg", "image/png"}:
            raise UploadConflict("unsupported_media_type")
        if request.byte_size > MAX_IMAGE_BYTES:
            raise UploadConflict("image_too_large")
        needs_multipart = False
        initialization_token = str(uuid4())
        initialization_generation = 0
        # Idempotent completed results remain readable after a task is sealed.
        async with UnitOfWork(self._session_factory) as read_unit_of_work:
            task = await TaskRepository(read_unit_of_work.session).get(user_id, task_id)
            if task is None:
                raise UploadNotFound("task_not_found")
            await self._require_upload_binding(
                read_unit_of_work.session,
                task=task,
                user_id=user_id,
                request=request,
            )
            existing = await AssetRepository(read_unit_of_work.session).for_task_local(
                user_id, task_id, request.local_asset_id
            )
            if existing is not None and existing.status in (
                AssetStatus.UPLOADED,
                AssetStatus.SKIPPED_DUPLICATE,
            ):
                if (
                    existing.status == AssetStatus.SKIPPED_DUPLICATE
                    and not self._same_idempotency_payload(existing, request)
                ):
                    raise UploadConflict("idempotency_payload_mismatch")
                return self._initiate_result(
                    existing,
                    await self._blob_for_asset(
                        AssetRepository(read_unit_of_work.session), user_id, existing
                    ),
                )
        async with UnitOfWork(self._session_factory) as unit_of_work:
            task_repository = TaskRepository(unit_of_work.session)
            repository = AssetRepository(unit_of_work.session)
            task = await task_repository.get_for_update(user_id, task_id)
            if task is None:
                raise UploadNotFound("task_not_found")
            await self._require_upload_binding(
                unit_of_work.session,
                task=task,
                user_id=user_id,
                request=request,
            )
            task = self._require_open_task(task)
            existing = await repository.for_task_local(
                user_id, task_id, request.local_asset_id
            )
            if existing is not None:
                if (
                    existing.status == AssetStatus.SKIPPED_DUPLICATE
                    and not self._same_idempotency_payload(existing, request)
                ):
                    raise UploadConflict("idempotency_payload_mismatch")
                if existing.status == AssetStatus.FAILED and request.retry:
                    if not self._same_idempotency_payload(existing, request):
                        raise UploadConflict("idempotency_payload_mismatch")
                    blob = (
                        await repository.blob(user_id, existing.blob_id)
                        if existing.blob_id
                        else None
                    )
                    if blob is None:
                        raise UploadConflict("upload_not_active")
                    existing.status = AssetStatus.PENDING
                    existing.failure_code = None
                    existing.failure_message = None
                    blob.provider_upload_id = None
                    blob.provider_etag = None
                    blob.provider_version_id = None
                    blob.completed_parts = None
                    blob.is_ready = False
                    blob.initialization_claim_id = initialization_token
                    blob.initialization_claimed_at = datetime.now(UTC)
                    blob.initialization_generation += 1
                    initialization_generation = blob.initialization_generation
                    asset, blob_for_upload = existing, blob
                    await self._refresh_counts(repository, task)
                    needs_multipart = True
                elif existing.status == AssetStatus.PENDING:
                    if not self._same_idempotency_payload(existing, request):
                        raise UploadConflict("idempotency_payload_mismatch")
                    blob = await self._blob_for_asset(
                        repository, user_id, existing, lock=True
                    )
                    if blob is None:
                        raise UploadConflict("upload_not_active")
                    stale_before = datetime.now(UTC) - timedelta(
                        seconds=self._initialization_lease_seconds
                    )
                    if blob.provider_upload_id is not None or (
                        blob.initialization_claim_id is not None
                        and blob.initialization_claimed_at is not None
                        and blob.initialization_claimed_at > stale_before
                    ):
                        return self._initiate_result(existing, blob)
                    blob.initialization_claim_id = initialization_token
                    blob.initialization_claimed_at = datetime.now(UTC)
                    blob.initialization_generation += 1
                    initialization_generation = blob.initialization_generation
                    asset, blob_for_upload = existing, blob
                    needs_multipart = True
                else:
                    return self._initiate_result(
                        existing,
                        await self._blob_for_asset(repository, user_id, existing),
                    )
            else:
                asset = Asset(
                    id=uuid4(),
                    user_id=user_id,
                    task_id=task_id,
                    local_asset_id=request.local_asset_id,
                    filename=request.filename,
                    media_type=request.media_type,
                    byte_size=request.byte_size,
                    sha256=request.sha256,
                    captured_at=request.captured_at,
                    status=AssetStatus.PENDING,
                )
                try:
                    admission = await TaskCapacityCoordinator(repository).admit(asset)
                except TaskCapacityExceeded:
                    raise UploadConflict("task_asset_limit")
                if admission.disposition == "existing":
                    return self._initiate_result(
                        admission.asset,
                        await self._blob_for_asset(
                            repository, user_id, admission.asset
                        ),
                    )
                if admission.disposition == "duplicate":
                    await self._refresh_counts(repository, task)
                    return self._initiate_result(admission.asset, None)

                ready_blob = await repository.ready_blob(
                    user_id, request.sha256, request.byte_size
                )
                if ready_blob is not None:
                    asset.blob_id = ready_blob.id
                    asset.status = AssetStatus.UPLOADED
                    self._mark_uploading(task)
                    await self._refresh_counts(repository, task)
                    return self._initiate_result(asset, ready_blob)
                object_key = f"{user_id}/{task_id}/{asset.id}/original"
                blob_for_upload = StoredBlob(
                    id=uuid4(),
                    user_id=user_id,
                    sha256=request.sha256,
                    byte_size=request.byte_size,
                    object_key=object_key,
                    initialization_claim_id=initialization_token,
                    initialization_claimed_at=datetime.now(UTC),
                    initialization_generation=1,
                )
                await repository.add(blob_for_upload)
                asset.blob_id = blob_for_upload.id
                self._mark_uploading(task)
                await self._refresh_counts(repository, task)
                needs_multipart = True
                initialization_generation = 1

        if not needs_multipart:
            raise AssertionError("initiation must return or create a multipart upload")
        owned = asyncio.create_task(
            self._capture_owned_initialization(
                user_id,
                asset.id,
                initialization_token,
                initialization_generation,
                blob_for_upload.object_key,
                request.media_type,
            )
        )
        self._owned_settlement_tasks.add(owned)
        owned.add_done_callback(self._observe_owned_settlement)
        (result, error), cancelled = await self._await_settlement(owned)
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error
        if result is None:
            raise AssertionError("owned initialization must return or raise")
        return result

    @staticmethod
    async def _require_upload_binding(
        session: AsyncSession,
        *,
        task: ImportTask,
        user_id: UUID,
        request: AssetInitRequest,
    ) -> None:
        if task.opc_id is None:
            if request.binding_id is not None or request.candidate_id is not None:
                raise UploadConflict("upload_binding_mismatch")
            return
        if request.binding_id is None or request.candidate_id is None:
            raise UploadConflict("upload_binding_required")
        binding = await session.scalar(
            select(ControlledUploadBinding)
            .where(
                ControlledUploadBinding.id == request.binding_id,
                ControlledUploadBinding.candidate_id == request.candidate_id,
                ControlledUploadBinding.opc_id == task.opc_id,
                ControlledUploadBinding.task_id == task.id,
                ControlledUploadBinding.user_id == user_id,
            )
            .with_for_update()
        )
        if binding is None or binding.candidate_payload is None:
            raise UploadConflict("upload_binding_mismatch")
        try:
            candidate = ControlledUploadCandidate.model_validate(
                binding.candidate_payload
            )
        except ValidationError:
            raise UploadConflict("upload_binding_mismatch") from None
        if (
            candidate.candidate_id != binding.candidate_id
            or candidate.opc_id != task.opc_id
            or candidate.album_product_id != binding.album_product_id
            or candidate.source_readiness_id != binding.source_readiness_id
            or candidate.source_type.value != binding.source_type
            or len(candidate.items) != binding.item_count
        ):
            raise UploadConflict("upload_binding_mismatch")
        item = next(
            (
                candidate_item
                for candidate_item in candidate.items
                if str(candidate_item.local_asset_id) == request.local_asset_id
            ),
            None,
        )
        if item is None or (
            request.filename != item.filename
            or request.media_type != item.media_type
            or request.byte_size != item.byte_size
            or request.sha256 != item.sha256
            or request.captured_at != item.captured_at
        ):
            raise UploadConflict("upload_binding_mismatch")

    async def _capture_owned_initialization(
        self,
        user_id: UUID,
        asset_id: UUID,
        initialization_token: str,
        initialization_generation: int,
        object_key: str,
        media_type: str,
    ) -> tuple[InitiateResult | None, Exception | None]:
        """Observe an owned exception without losing caller cancellation precedence."""
        try:
            return (
                await self._initialize_multipart_owned(
                    user_id,
                    asset_id,
                    initialization_token,
                    initialization_generation,
                    object_key,
                    media_type,
                ),
                None,
            )
        except Exception as error:  # noqa: BLE001 - re-raised after settlement.
            return None, error

    async def _initialize_multipart_owned(
        self,
        user_id: UUID,
        asset_id: UUID,
        initialization_token: str,
        initialization_generation: int,
        object_key: str,
        media_type: str,
    ) -> InitiateResult:
        creation = asyncio.create_task(
            self._capture_provider_creation(object_key, media_type)
        )
        (
            (provider_upload_id, creation_error),
            creation_cancelled,
        ) = await self._await_settlement(creation)
        if creation_error is not None:
            await self._record_initialization_failure(
                user_id, asset_id, initialization_token, initialization_generation
            )
            if creation_cancelled:
                raise asyncio.CancelledError
            raise creation_error
        if provider_upload_id is None:
            raise AssertionError("provider creation must return or raise")
        if creation_cancelled:
            compensation = asyncio.create_task(
                self._settle_initialization_compensation(
                    user_id,
                    asset_id,
                    initialization_token,
                    initialization_generation,
                    object_key,
                    provider_upload_id,
                    attempts=None,
                )
            )
            await self._await_settlement(compensation)
            raise asyncio.CancelledError

        provider_was_published = False
        try:
            try:
                cleanup_job_id = await self._record_initialization_cleanup(
                    user_id, asset_id, object_key, provider_upload_id
                )
            except Exception as cleanup_error:
                await self._settle_initialization_compensation(
                    user_id,
                    asset_id,
                    initialization_token,
                    initialization_generation,
                    object_key,
                    provider_upload_id,
                    attempts=None,
                )
                raise UploadConflict(
                    "upload_initialization_recovery_required"
                ) from cleanup_error
            publication = asyncio.create_task(
                self._capture_initialized_publication(
                    user_id,
                    asset_id,
                    initialization_token,
                    initialization_generation,
                    provider_upload_id,
                    cleanup_job_id,
                )
            )
            (
                (result, publication_error),
                publication_cancelled,
            ) = await self._await_settlement(publication)
            if publication_error is not None:
                if publication_cancelled:
                    raise asyncio.CancelledError
                raise publication_error
            if result is None:
                raise AssertionError("upload publication must return or raise")
            provider_was_published = result.provider_upload_id == provider_upload_id
            if publication_cancelled:
                raise asyncio.CancelledError
            if result.provider_upload_id != provider_upload_id:
                await self.process_cleanup_jobs(user_id)
            return result
        except asyncio.CancelledError:
            if not provider_was_published:
                compensation = asyncio.create_task(
                    self._settle_initialization_compensation(
                        user_id,
                        asset_id,
                        initialization_token,
                        initialization_generation,
                        object_key,
                        provider_upload_id,
                        attempts=None,
                    )
                )
                await self._await_settlement(compensation)
            raise

    async def _capture_provider_creation(
        self, object_key: str, media_type: str
    ) -> tuple[str | None, Exception | None]:
        """Retain a late provider result even if its owned initializer is cancelled."""
        try:
            return await self.storage.create_multipart(object_key, media_type), None
        except Exception as error:  # noqa: BLE001 - re-raised after observation.
            return None, error

    async def _capture_initialized_publication(
        self,
        user_id: UUID,
        asset_id: UUID,
        initialization_token: str,
        initialization_generation: int,
        provider_upload_id: str,
        cleanup_job_id: UUID,
    ) -> tuple[InitiateResult | None, Exception | None]:
        """Observe publication through commit before classifying cancellation."""
        try:
            return (
                await self._persist_initialized_upload(
                    user_id,
                    asset_id,
                    initialization_token,
                    initialization_generation,
                    provider_upload_id,
                    cleanup_job_id,
                ),
                None,
            )
        except Exception as error:  # noqa: BLE001 - re-raised after observation.
            return None, error

    async def sign_part(
        self, user_id: UUID, asset_id: UUID, part_number: int
    ) -> SignedPart:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            task = await TaskRepository(unit_of_work.session).get_for_update(
                user_id, asset.task_id
            )
            self._require_open_task(task)
            blob = await self._blob_for_asset(repository, user_id, asset)
            if (
                asset.status != AssetStatus.UPLOADING
                or blob is None
                or blob.provider_upload_id is None
            ):
                raise UploadConflict("upload_not_active")
            object_key, provider_upload_id = blob.object_key, blob.provider_upload_id
        url = await self.storage.sign_part(
            object_key, provider_upload_id, part_number, PART_URL_TTL_SECONDS
        )
        return SignedPart(url)

    async def complete(
        self, user_id: UUID, asset_id: UUID, parts: list[CompletedPart]
    ) -> CompleteResult:
        normalized_parts = self._validate_parts(parts)
        for _ in range(30):
            claim = await self._claim_completion(user_id, asset_id)
            if isinstance(claim, Asset):
                return CompleteResult(claim)
            if claim is None:
                reconciled = await self._reconcile_completed_object(user_id, asset_id)
                if reconciled is not None:
                    return CompleteResult(reconciled)
                await asyncio.sleep(0.02)
                continue
            token, generation, object_key, provider_upload_id, expected_size = claim
            heartbeat = asyncio.create_task(
                self._renew_completion_claim(user_id, asset_id, token, generation)
            )
            heartbeat_error: Exception | None = None
            provider_error: Exception | None = None
            cancelled = False
            try:
                head, cancelled = await self._complete_provider_operation(
                    object_key, provider_upload_id, normalized_parts
                )
            except Exception as error:  # noqa: BLE001 - normalized below after ownership settlement.
                provider_error = error
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                except Exception as error:  # noqa: BLE001 - renewal loss makes ownership uncertain
                    heartbeat_error = error
            if heartbeat_error is not None:
                # Do not finalize from a claimant that could not prove its
                # lease renewal.  A subsequent retry reconciles object head
                # under the durable fencing token.
                raise UploadConflict("upload_claim_uncertain") from heartbeat_error
            if provider_error is not None:
                reconciled = await self._reconcile_completed_object(user_id, asset_id)
                if reconciled is not None:
                    return CompleteResult(reconciled)
                await self._release_completion_claim(
                    user_id, asset_id, token, generation
                )
                if isinstance(provider_error, StorageNotFound):
                    raise UploadConflict("upload_not_active") from None
                raise provider_error
            if cancelled:
                # Provider work is settled and observed; hand durable recovery
                # to a later caller instead of doing more request work.
                raise asyncio.CancelledError
            if head.byte_size != expected_size:
                await self._record_size_mismatch_and_enqueue_delete(
                    user_id, asset_id, object_key
                )
                await self._best_effort_cleanup_drain(user_id)
                raise UploadConflict("object_size_mismatch")
            try:
                finalized = await self._finalize_completion(
                    user_id, asset_id, token, generation, head, normalized_parts
                )
            except UploadConflict as error:
                if str(error) == "upload_not_active":
                    reconciled = await self._reconcile_completed_object(
                        user_id, asset_id
                    )
                    if reconciled is not None:
                        return CompleteResult(reconciled)
                # A task may have transitioned terminally after the provider
                # call started.  Only a terminal lifecycle gate owns deletion;
                # an old fenced claimant must never delete a winner's object.
                if str(error) in {"task_sealed", "upload_not_active"}:
                    await self._mark_terminal_reconciliation_and_enqueue_delete(
                        user_id, asset_id, object_key
                    )
                    await self._best_effort_cleanup_drain(user_id)
                raise
            return CompleteResult(finalized)
        raise UploadConflict("upload_not_active")

    async def _claim_completion(
        self, user_id: UUID, asset_id: UUID
    ) -> Asset | tuple[str, int, str, str, int] | None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            if asset.status == AssetStatus.UPLOADED:
                return asset
            task = await TaskRepository(unit_of_work.session).get_for_update(
                user_id, asset.task_id
            )
            self._require_open_task(task)
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            if asset.status == AssetStatus.UPLOADED:
                return asset
            if asset.status != AssetStatus.UPLOADING:
                raise UploadConflict("upload_not_active")
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if blob is None or blob.provider_upload_id is None:
                raise UploadConflict("upload_not_active")
            # Claims may be reclaimed after process loss.  The provider
            # advisory lock below, not this timestamp, serializes live calls.
            stale_before = datetime.now(UTC) - timedelta(
                seconds=self._completion_lease_seconds
            )
            if (
                blob.completion_claim_id
                and blob.completion_claimed_at
                and blob.completion_claimed_at > stale_before
            ):
                return None
            token = str(uuid4())
            blob.completion_claim_id = token
            blob.completion_claimed_at = datetime.now(UTC)
            blob.completion_generation += 1
            await unit_of_work.session.flush()
            return (
                token,
                blob.completion_generation,
                blob.object_key,
                blob.provider_upload_id,
                blob.byte_size,
            )

    async def _reconcile_completed_object(
        self, user_id: UUID, asset_id: UUID
    ) -> Asset | None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            if asset.status == AssetStatus.UPLOADED:
                return asset
            blob = await self._blob_for_asset(repository, user_id, asset)
            if blob is None:
                return None
            object_key, expected_size = blob.object_key, blob.byte_size
            token = blob.completion_claim_id
        try:
            head, _ = await self._run_provider_serialized(
                object_key, lambda: self.storage.head_object(object_key)
            )
        except Exception:  # noqa: BLE001 - not-found differs between S3-compatible providers.
            return None
        if token is None:
            return None
        if head.byte_size != expected_size:
            await self._record_size_mismatch_and_enqueue_delete(
                user_id, asset_id, object_key
            )
            await self._best_effort_cleanup_drain(user_id)
            raise UploadConflict("object_size_mismatch")
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            blob = await self._blob_for_asset(repository, user_id, asset)
            if blob is None or blob.completion_claim_id != token:
                return None
            generation = blob.completion_generation
        try:
            return await self._finalize_completion(
                user_id, asset_id, token, generation, head, []
            )
        except UploadConflict:
            await self._mark_terminal_reconciliation_and_enqueue_delete(
                user_id, asset_id, object_key
            )
            await self._best_effort_cleanup_drain(user_id)
            return None

    async def _finalize_completion(
        self,
        user_id: UUID,
        asset_id: UUID,
        token: str,
        generation: int,
        head: ObjectHead,
        parts: list[MultipartPart],
    ) -> Asset:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            task = await TaskRepository(unit_of_work.session).get_for_update(
                user_id, asset.task_id
            )
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            if asset.status == AssetStatus.UPLOADED:
                return asset
            self._require_open_task(task)
            if asset.status != AssetStatus.UPLOADING:
                raise UploadConflict("upload_not_active")
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if blob is None:
                raise UploadConflict("upload_not_active")
            if (
                blob.completion_claim_id != token
                or blob.completion_generation != generation
            ):
                raise UploadConflict("upload_not_active")
            if head.byte_size != blob.byte_size:
                raise UploadConflict("object_size_mismatch")
            await repository.lock_digest(user_id, blob.sha256, blob.byte_size)
            winner = await repository.ready_blob(user_id, blob.sha256, blob.byte_size)
            if winner is not None and winner.id != blob.id:
                asset.blob_id = winner.id
                blob.cleanup_pending = True
                unit_of_work.session.add(
                    StorageCleanupJob(
                        user_id=user_id,
                        blob_id=blob.id,
                        operation="delete_object",
                        object_key=blob.object_key,
                    )
                )
            else:
                blob.provider_etag = head.etag
                blob.provider_version_id = head.version_id
                blob.completed_parts = json.dumps(
                    [
                        {"part_number": part.part_number, "etag": part.etag}
                        for part in parts
                    ],
                    separators=(",", ":"),
                )
                blob.is_ready = True
            blob.completion_claim_id = None
            blob.completion_claimed_at = None
            asset.status = AssetStatus.UPLOADED
            if task is not None:
                await self._refresh_counts(repository, task)
            await unit_of_work.session.flush()
            await unit_of_work.session.refresh(asset)
            return asset

    async def _release_completion_claim(
        self, user_id: UUID, asset_id: UUID, token: str, generation: int
    ) -> None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None:
                return
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if (
                blob is not None
                and blob.completion_claim_id == token
                and blob.completion_generation == generation
            ):
                blob.completion_claim_id = None
                blob.completion_claimed_at = None

    async def _renew_completion_claim(
        self, user_id: UUID, asset_id: UUID, token: str, generation: int
    ) -> None:
        while True:
            await asyncio.sleep(self._completion_lease_seconds / 4)
            async with UnitOfWork(self._session_factory) as unit_of_work:
                repository = AssetRepository(unit_of_work.session)
                asset = await repository.get(user_id, asset_id, lock=True)
                if asset is None:
                    return
                blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
                if (
                    blob is None
                    or blob.completion_claim_id != token
                    or blob.completion_generation != generation
                ):
                    return
                blob.completion_claimed_at = datetime.now(UTC)

    async def fail(
        self, user_id: UUID, asset_id: UUID, code: str, message: str
    ) -> Asset:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            task_repository = TaskRepository(unit_of_work.session)
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            if asset.status == AssetStatus.FAILED:
                return asset
            if asset.status not in (AssetStatus.PENDING, AssetStatus.UPLOADING):
                raise UploadConflict("upload_not_active")
            task = await task_repository.get_for_update(user_id, asset.task_id)
            task = self._require_open_task(task)
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            if asset.status == AssetStatus.UPLOADED:
                return asset
            if asset.status not in (AssetStatus.PENDING, AssetStatus.UPLOADING):
                raise UploadConflict("upload_not_active")
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            object_key = blob.object_key if blob is not None else None
            provider_upload_id = blob.provider_upload_id if blob is not None else None
            asset.status = AssetStatus.FAILED
            asset.failure_code = code
            asset.failure_message = message
            await self._refresh_counts(repository, task)
            await unit_of_work.session.flush()
            await unit_of_work.session.refresh(asset)
        if object_key is not None and provider_upload_id is not None:
            await self._cleanup_abort(user_id, asset_id, object_key, provider_upload_id)
        return asset

    async def unsupported(
        self, user_id: UUID, task_id: UUID, request: UnsupportedAssetRequest
    ) -> Asset:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            task_repository = TaskRepository(unit_of_work.session)
            repository = AssetRepository(unit_of_work.session)
            task = await task_repository.get_for_update(user_id, task_id)
            task = self._require_open_task(task)
            existing = await repository.for_task_local(
                user_id, task_id, request.local_asset_id
            )
            if existing is not None:
                return existing
            asset = Asset(
                id=uuid4(),
                user_id=user_id,
                task_id=task_id,
                local_asset_id=request.local_asset_id,
                filename=request.filename,
                media_type=request.media_type,
                byte_size=request.byte_size,
                status=AssetStatus.SKIPPED_UNSUPPORTED,
                unsupported_reason=request.reason,
            )
            await repository.add(asset)
            await self._refresh_counts(repository, task)
            return asset

    async def _record_initialization_cleanup(
        self, user_id: UUID, asset_id: UUID, object_key: str, provider_upload_id: str
    ) -> UUID:
        """Persist a compensating abort before attempting provider-ID ownership CAS."""
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            blob = (
                await self._blob_for_asset(repository, user_id, asset, lock=True)
                if asset is not None
                else None
            )
            if blob is not None:
                blob.cleanup_pending = True
            job = StorageCleanupJob(
                user_id=user_id,
                blob_id=blob.id if blob is not None else None,
                operation="abort_multipart",
                object_key=object_key,
                provider_upload_id=provider_upload_id,
            )
            unit_of_work.session.add(job)
            await unit_of_work.session.flush()
            return job.id

    async def _persist_initialized_upload(
        self,
        user_id: UUID,
        asset_id: UUID,
        token: str,
        generation: int,
        provider_upload_id: str,
        cleanup_job_id: UUID,
    ) -> InitiateResult:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            task = await TaskRepository(unit_of_work.session).get_for_update(
                user_id, asset.task_id
            )
            self._require_open_task(task)
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if (
                asset.status != AssetStatus.PENDING
                or blob is None
                or blob.initialization_claim_id != token
                or blob.initialization_generation != generation
            ):
                return self._initiate_result(asset, blob)
            job = await unit_of_work.session.get(
                StorageCleanupJob, cleanup_job_id, with_for_update=True
            )
            # Absence is deliberately not accepted: without the committed
            # compensation record, a provider upload could become untracked.
            if job is None or job.status != "pending" or job.claim_id is not None:
                raise UploadConflict("upload_initialization_recovery_required")
            blob.provider_upload_id = provider_upload_id
            blob.initialization_claim_id = None
            blob.initialization_claimed_at = None
            blob.cleanup_pending = False
            asset.status = AssetStatus.UPLOADING
            await unit_of_work.session.delete(job)
            await unit_of_work.session.flush()
            await unit_of_work.session.refresh(asset)
            return self._initiate_result(asset, blob)

    async def _claim_initialization(
        self, user_id: UUID, asset_id: UUID
    ) -> tuple[str, int] | None:
        """Claim a stale PENDING initializer with a generation fence for recovery."""
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                raise UploadNotFound("asset_not_found")
            task = await TaskRepository(unit_of_work.session).get_for_update(
                user_id, asset.task_id
            )
            self._require_open_task(task)
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None or asset.status != AssetStatus.PENDING:
                return None
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if blob is None or blob.provider_upload_id is not None:
                return None
            stale_before = datetime.now(UTC) - timedelta(
                seconds=self._initialization_lease_seconds
            )
            if (
                blob.initialization_claim_id is not None
                and blob.initialization_claimed_at is not None
                and blob.initialization_claimed_at > stale_before
            ):
                return None
            token = str(uuid4())
            blob.initialization_claim_id = token
            blob.initialization_claimed_at = datetime.now(UTC)
            blob.initialization_generation += 1
            await unit_of_work.session.flush()
            return token, blob.initialization_generation

    async def _record_initialization_failure(
        self, user_id: UUID, asset_id: UUID, token: str, generation: int
    ) -> None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            task_repository = TaskRepository(unit_of_work.session)
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                return
            task = await task_repository.get_for_update(user_id, asset.task_id)
            if task is None:
                return
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None or asset.status != AssetStatus.PENDING:
                return
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if (
                blob is None
                or blob.initialization_claim_id != token
                or blob.initialization_generation != generation
            ):
                return
            asset.status = AssetStatus.FAILED
            asset.failure_code = "upload_initialization_failed"
            asset.failure_message = "Unable to start the upload. Please retry."
            blob.initialization_claim_id = None
            blob.initialization_claimed_at = None
            await self._refresh_counts(repository, task)

    async def _record_size_mismatch(self, user_id: UUID, asset_id: UUID) -> None:
        await self._record_size_mismatch_and_enqueue_delete(user_id, asset_id, None)

    async def _record_size_mismatch_and_enqueue_delete(
        self, user_id: UUID, asset_id: UUID, object_key: str | None
    ) -> None:
        """Fail and enqueue deletion together; callers may process only after commit."""
        async with UnitOfWork(self._session_factory) as unit_of_work:
            task_repository = TaskRepository(unit_of_work.session)
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                return
            task = await task_repository.get_for_update(user_id, asset.task_id)
            if task is None:
                return
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None or asset.status != AssetStatus.UPLOADING:
                return
            asset.status = AssetStatus.FAILED
            asset.failure_code = "object_size_mismatch"
            asset.failure_message = (
                "Uploaded object size does not match the requested size."
            )
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if blob is not None:
                blob.cleanup_pending = True
                unit_of_work.session.add(
                    StorageCleanupJob(
                        user_id=user_id,
                        blob_id=blob.id,
                        operation="delete_object",
                        object_key=object_key or blob.object_key,
                    )
                )
            await self._refresh_counts(repository, task)

    async def _record_initialization_cleanup_recovery(
        self,
        user_id: UUID,
        asset_id: UUID,
        token: str,
        generation: int,
        object_key: str,
        provider_upload_id: str,
        *,
        retain_provider_upload: bool,
    ) -> bool:
        """Persist exact compensation independently of the initializer fence."""
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id, lock=True)
            blob = (
                await self._blob_for_asset(repository, user_id, asset, lock=True)
                if asset
                else None
            )
            if blob is None or blob.object_key != object_key:
                blob = await unit_of_work.session.scalar(
                    select(StoredBlob)
                    .where(
                        StoredBlob.user_id == user_id,
                        StoredBlob.object_key == object_key,
                    )
                    .with_for_update()
                )
            if blob is None:
                return False
            owns_initializer = (
                blob.initialization_claim_id == token
                and blob.initialization_generation == generation
            )
            if retain_provider_upload:
                if blob.provider_upload_id in (None, provider_upload_id):
                    blob.provider_upload_id = provider_upload_id
                    blob.cleanup_pending = True
                existing = await unit_of_work.session.scalar(
                    select(StorageCleanupJob).where(
                        StorageCleanupJob.user_id == user_id,
                        StorageCleanupJob.operation == "abort_multipart",
                        StorageCleanupJob.object_key == object_key,
                        StorageCleanupJob.provider_upload_id == provider_upload_id,
                    )
                )
                if existing is None:
                    unit_of_work.session.add(
                        StorageCleanupJob(
                            user_id=user_id,
                            blob_id=blob.id,
                            operation="abort_multipart",
                            object_key=object_key,
                            provider_upload_id=provider_upload_id,
                        )
                    )
            elif owns_initializer:
                blob.initialization_claim_id = None
                blob.initialization_claimed_at = None
                if blob.provider_upload_id is None:
                    blob.cleanup_pending = False
            return True

    async def _settle_initialization_compensation(
        self,
        user_id: UUID,
        asset_id: UUID,
        token: str,
        generation: int,
        object_key: str,
        provider_upload_id: str,
        *,
        attempts: int | None,
    ) -> bool:
        """Persist abort work or prove abort absent; never silently lose the ID."""

        async def persist(retain_provider_upload: bool) -> bool:
            try:
                return await self._record_initialization_cleanup_recovery(
                    user_id,
                    asset_id,
                    token,
                    generation,
                    object_key,
                    provider_upload_id,
                    retain_provider_upload=retain_provider_upload,
                )
            except Exception:  # noqa: BLE001 - DB may be the original failure.
                return False

        attempt = 0
        while attempts is None or attempt < attempts:
            attempt += 1
            try:
                _, _ = await self._run_provider_serialized(
                    object_key,
                    lambda: self.storage.abort_multipart(
                        object_key, provider_upload_id
                    ),
                )
            except StorageNotFound:
                if await persist(False):
                    return True
            except Exception:  # noqa: BLE001 - persist exact provider ID for later cleanup.
                if await persist(True):
                    return True
            else:
                if await persist(False):
                    return True
            await asyncio.sleep(PROVIDER_LOCK_BACKOFF_SECONDS * attempt)
        return False

    def _observe_owned_settlement(self, task: asyncio.Task[object]) -> None:
        self._owned_settlement_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - callback observes an owned task at shutdown.
            return

    async def _complete_provider_operation(
        self,
        object_key: str,
        provider_upload_id: str,
        parts: list[MultipartPart],
    ) -> tuple[ObjectHead, bool]:
        """Settle external work before honoring cancellation of the HTTP request."""

        async def operation() -> ObjectHead:
            # A waiter acquires the advisory lock only after the original
            # provider call settles.  Re-check while holding it, so it never
            # invokes complete on an upload that the prior owner consumed.
            try:
                return await self.storage.head_object(object_key)
            except StorageNotFound:
                await self.storage.complete_multipart(
                    object_key, provider_upload_id, parts
                )
                return await self.storage.head_object(object_key)

        operation_task = asyncio.create_task(
            self._run_provider_serialized(object_key, operation)
        )
        (result, _locked), cancelled = await self._await_settlement(operation_task)
        return result, cancelled

    @staticmethod
    async def _await_settlement(task: asyncio.Task[T]) -> tuple[T, bool]:
        """Observe an owned provider task through every request cancellation."""
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True

    async def _run_provider_serialized(
        self, object_key: str, operation: Callable[[], Awaitable[T]]
    ) -> tuple[T, bool]:
        """Hold a dedicated PostgreSQL advisory lock throughout provider I/O.

        The session connection is intentionally separate from asset state UoWs.
        It survives request-coroutine cancellation because callers shield this
        task; process death closes the connection and releases the lock.
        """
        for attempt in range(self._provider_lock_attempts):
            # Every losing contender closes this session before its backoff, so
            # a constrained DB pool remains available to heartbeats and UoWs.
            async with self._session_factory() as session:
                connection = await session.connection()
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                    {"key": f"multipart-complete:{object_key}"},
                )
                if acquired:
                    try:
                        return await operation(), True
                    finally:
                        await connection.execute(
                            text(
                                "SELECT pg_advisory_unlock(hashtextextended(:key, 0))"
                            ),
                            {"key": f"multipart-complete:{object_key}"},
                        )
            await asyncio.sleep(PROVIDER_LOCK_BACKOFF_SECONDS * (attempt + 1))
        raise StorageTransient("storage_transient")

    async def _best_effort_cleanup_drain(self, user_id: UUID) -> None:
        """Never let post-commit cleanup infrastructure replace a business result."""
        try:
            await self.process_cleanup_jobs(user_id)
        except Exception:  # noqa: BLE001, S110 - durable work is already committed.
            pass

    async def _mark_terminal_reconciliation_and_enqueue_delete(
        self, user_id: UUID, asset_id: UUID, object_key: str
    ) -> None:
        """Terminal task after provider completion: persist failure and cleanup first."""
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id)
            if asset is None:
                return
            task = await TaskRepository(unit_of_work.session).get_for_update(
                user_id, asset.task_id
            )
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None:
                return
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if blob is None:
                return
            # A fencing conflict is not ownership of the object.  A still-open
            # UPLOADING asset might belong to a newer claimant, so it cannot
            # trigger deletion.  A FAILED asset is a deliberate terminal
            # result (for example `fail()` won this race) and is safe.
            task_terminal = task is None or task.status not in (
                TaskStatus.WAITING_UPLOAD,
                TaskStatus.UPLOADING,
            )
            if asset.status == AssetStatus.UPLOADING and not task_terminal:
                return
            if asset.status not in (AssetStatus.UPLOADING, AssetStatus.FAILED):
                return
            blob.cleanup_pending = True
            if asset.status == AssetStatus.UPLOADING:
                asset.status = AssetStatus.FAILED
                asset.failure_code = "task_sealed_after_upload"
                asset.failure_message = (
                    "Task closed before upload completion was recorded."
                )
                if task is not None:
                    await self._refresh_counts(repository, task)
            unit_of_work.session.add(
                StorageCleanupJob(
                    user_id=user_id,
                    blob_id=blob.id,
                    operation="delete_object",
                    object_key=object_key,
                )
            )

    async def _cleanup_abort(
        self, user_id: UUID, asset_id: UUID, object_key: str, provider_upload_id: str
    ) -> None:
        await self._enqueue_cleanup(
            user_id, asset_id, "abort_multipart", object_key, provider_upload_id
        )
        await self.process_cleanup_jobs(user_id)

    async def _cleanup_object(
        self, user_id: UUID, asset_id: UUID, object_key: str
    ) -> None:
        await self._enqueue_cleanup(
            user_id, asset_id, "delete_object", object_key, None
        )
        await self.process_cleanup_jobs(user_id)

    async def _enqueue_cleanup(
        self,
        user_id: UUID,
        asset_id: UUID,
        operation: str,
        object_key: str,
        provider_upload_id: str | None,
    ) -> None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            repository = AssetRepository(unit_of_work.session)
            asset = await repository.get(user_id, asset_id, lock=True)
            if asset is None:
                return
            blob = await self._blob_for_asset(repository, user_id, asset, lock=True)
            if blob is not None:
                blob.cleanup_pending = True
                unit_of_work.session.add(
                    StorageCleanupJob(
                        user_id=user_id,
                        blob_id=blob.id,
                        operation=operation,
                        object_key=object_key,
                        provider_upload_id=provider_upload_id,
                    )
                )

    async def process_cleanup_jobs(
        self, user_id: UUID | None = None, *, limit: int = 100
    ) -> None:
        """Run one bounded, durable cleanup pass without provider I/O under row locks."""
        if limit < 1:
            raise ValueError("cleanup limit must be positive")
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=30)
        async with UnitOfWork(self._session_factory) as unit_of_work:
            statement = select(StorageCleanupJob)
            if user_id is not None:
                statement = statement.where(StorageCleanupJob.user_id == user_id)
            statement = statement.where(
                (StorageCleanupJob.status == "pending")
                | (
                    (StorageCleanupJob.status == "processing")
                    & (StorageCleanupJob.claimed_at < stale_before)
                )
            )
            claimed: list[tuple[UUID, str, int, str, str, str | None]] = []
            for job in (
                await unit_of_work.session.scalars(
                    statement.order_by(StorageCleanupJob.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all():
                token = str(uuid4())
                job.status = "processing"
                job.claim_id = token
                job.claimed_at = now
                job.claim_generation += 1
                claimed.append(
                    (
                        job.id,
                        token,
                        job.claim_generation,
                        job.operation,
                        job.object_key,
                        job.provider_upload_id,
                    )
                )
        for (
            job_id,
            token,
            generation,
            operation,
            object_key,
            provider_upload_id,
        ) in claimed:
            try:
                await self._execute_cleanup_serialized(
                    operation, object_key, provider_upload_id
                )
            except StoragePermanent:
                async with UnitOfWork(self._session_factory) as unit_of_work:
                    claimed_job = await unit_of_work.session.get(
                        StorageCleanupJob, job_id, with_for_update=True
                    )
                    if (
                        claimed_job is not None
                        and claimed_job.claim_id == token
                        and claimed_job.claim_generation == generation
                    ):
                        claimed_job.attempts += 1
                        claimed_job.status = "dead"
                        claimed_job.terminal_code = "storage_permanent"
                        claimed_job.claim_id = None
                        claimed_job.claimed_at = None
                continue
            except Exception:  # noqa: BLE001 - provider retry state is durable.
                async with UnitOfWork(self._session_factory) as unit_of_work:
                    claimed_job = await unit_of_work.session.get(
                        StorageCleanupJob, job_id, with_for_update=True
                    )
                    if (
                        claimed_job is not None
                        and claimed_job.claim_id == token
                        and claimed_job.claim_generation == generation
                    ):
                        claimed_job.attempts += 1
                        claimed_job.status = "pending"
                        claimed_job.claim_id = None
                        claimed_job.claimed_at = None
                continue
            async with UnitOfWork(self._session_factory) as unit_of_work:
                claimed_job = await unit_of_work.session.get(
                    StorageCleanupJob, job_id, with_for_update=True
                )
                if (
                    claimed_job is None
                    or claimed_job.claim_id != token
                    or claimed_job.claim_generation != generation
                ):
                    continue
                blob_id = claimed_job.blob_id
                operation = claimed_job.operation
                await unit_of_work.session.delete(claimed_job)
                await unit_of_work.session.flush()
                if blob_id is not None:
                    remaining = await unit_of_work.session.scalar(
                        select(func.count())
                        .select_from(StorageCleanupJob)
                        .where(StorageCleanupJob.blob_id == blob_id)
                    )
                    if not remaining:
                        blob = await unit_of_work.session.get(StoredBlob, blob_id)
                        if blob is not None:
                            if (
                                operation == "abort_multipart"
                                and blob.provider_upload_id
                                == claimed_job.provider_upload_id
                            ):
                                blob.provider_upload_id = None
                            blob.cleanup_pending = False

    async def _execute_cleanup_operation(
        self, operation: str, object_key: str, provider_upload_id: str | None
    ) -> None:
        """Settle abort, absent-upload reconciliation, and delete as one retry unit."""
        if operation == "delete_object":
            try:
                await self.storage.delete_object(object_key)
            except StorageNotFound:
                pass
            return
        if operation != "abort_multipart" or provider_upload_id is None:
            raise StoragePermanent("storage_permanent")
        try:
            await self.storage.abort_multipart(object_key, provider_upload_id)
            return
        except StorageNotFound:
            pass
        try:
            await self.storage.head_object(object_key)
        except StorageNotFound:
            return
        try:
            await self.storage.delete_object(object_key)
        except StorageNotFound:
            return

    async def _execute_cleanup_serialized(
        self, operation: str, object_key: str, provider_upload_id: str | None
    ) -> None:
        """Do not wait behind a live completion: leave claimed cleanup retryable."""
        async with self._session_factory() as session:
            connection = await session.connection()
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": f"multipart-complete:{object_key}"},
            )
            if not acquired:
                raise StorageTransient("storage_transient")
            try:
                await self._execute_cleanup_operation(
                    operation, object_key, provider_upload_id
                )
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": f"multipart-complete:{object_key}"},
                )

    @staticmethod
    def _require_open_task(task: ImportTask | None) -> ImportTask:
        if task is None:
            raise UploadNotFound("task_not_found")
        if task.status not in (TaskStatus.WAITING_UPLOAD, TaskStatus.UPLOADING):
            raise UploadConflict("task_sealed")
        return task

    @staticmethod
    def _mark_uploading(task: ImportTask) -> None:
        if task.status == TaskStatus.WAITING_UPLOAD:
            task.status = TaskStatus.UPLOADING

    @staticmethod
    async def _blob_for_asset(
        repository: AssetRepository, user_id: UUID, asset: Asset, *, lock: bool = False
    ) -> StoredBlob | None:
        if asset.blob_id is None:
            return None
        return await repository.blob(user_id, asset.blob_id, lock=lock)

    @staticmethod
    def _initiate_result(asset: Asset, blob: StoredBlob | None) -> InitiateResult:
        completed = []
        if blob is not None and blob.completed_parts:
            completed = [
                int(part["part_number"]) for part in json.loads(blob.completed_parts)
            ]
        is_duplicate = asset.status == AssetStatus.SKIPPED_DUPLICATE
        return InitiateResult(
            asset=asset,
            already_uploaded=asset.status == AssetStatus.UPLOADED or is_duplicate,
            provider_upload_id=(
                blob.provider_upload_id
                if blob is not None and asset.status == AssetStatus.UPLOADING
                else None
            ),
            completed_parts=completed,
            disposition="duplicate" if is_duplicate else "accepted",
            canonical_asset_id=asset.canonical_asset_id if is_duplicate else None,
        )

    @staticmethod
    async def _refresh_counts(repository: AssetRepository, task: ImportTask) -> None:
        total, uploaded, failed, unsupported = await repository.counts(
            task.user_id, task.id
        )
        task.total_assets = total
        task.uploaded_assets = uploaded
        task.failed_assets = failed
        task.unsupported_assets = unsupported

    @staticmethod
    def _validate_parts(parts: list[CompletedPart]) -> list[MultipartPart]:
        if not 1 <= len(parts) <= 10_000:
            raise InvalidParts("invalid_parts")
        numbers = [part.part_number for part in parts]
        if (
            numbers != sorted(numbers)
            or len(numbers) != len(set(numbers))
            or any(not 1 <= number <= 10_000 for number in numbers)
            or any(not 1 <= len(part.etag) <= 256 for part in parts)
        ):
            raise InvalidParts("invalid_parts")
        return [MultipartPart(part.part_number, part.etag) for part in parts]

    @staticmethod
    def _same_idempotency_payload(asset: Asset, request: AssetInitRequest) -> bool:
        return (
            asset.filename == request.filename
            and asset.media_type == request.media_type
            and asset.byte_size == request.byte_size
            and asset.sha256 == request.sha256
            and asset.captured_at == request.captured_at
        )
