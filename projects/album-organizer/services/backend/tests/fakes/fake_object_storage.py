import asyncio
from collections.abc import Sequence

from photo_organizer.storage.object_storage import (
    MultipartPart,
    ObjectHead,
    StorageNotFound,
    StorageTransient,
)


class FakeObjectStorage:
    """In-memory ObjectStorage double with the production async contract."""

    def __init__(
        self,
        *,
        completed_size: int = 8,
        complete_delay_seconds: float = 0,
        create_delay_seconds: float = 0,
        fail_create_times: int = 0,
        fail_abort_times: int = 0,
        fail_delete_times: int = 0,
        block_create: bool = False,
        block_complete: bool = False,
        block_abort: bool = False,
    ) -> None:
        self.completed_size = completed_size
        self.complete_call_count = 0
        self.complete_attempt_count = 0
        self.active_complete_calls = 0
        self.max_concurrent_complete_calls = 0
        self.sign_call_count = 0
        self.download_sign_call_count = 0
        self.download_sign_calls: list[str] = []
        self.create_call_count = 0
        self.abort_call_count = 0
        self.delete_call_count = 0
        self.head_call_count = 0
        self.operation_log: list[tuple[str, str]] = []
        self.delete_calls_by_key: dict[str, int] = {}
        self._uploads: dict[str, tuple[str, str]] = {}
        self._heads: dict[str, ObjectHead] = {}
        self._next_upload = 1
        self.complete_delay_seconds = complete_delay_seconds
        self.create_delay_seconds = create_delay_seconds
        self.fail_create_times = fail_create_times
        self.fail_abort_times = fail_abort_times
        self.fail_delete_times = fail_delete_times
        self.create_started = asyncio.Event()
        self.create_release = asyncio.Event()
        if not block_create:
            self.create_release.set()
        self.complete_started = asyncio.Event()
        self.complete_release = asyncio.Event()
        if not block_complete:
            self.complete_release.set()
        self.abort_started = asyncio.Event()
        self.abort_release = asyncio.Event()
        if not block_abort:
            self.abort_release.set()
        self._faults: dict[str, list[type[Exception]]] = {}

    def inject_fault(
        self, operation: str, error: type[Exception], *, times: int = 1
    ) -> None:
        """Deterministically fail a named provider operation without provider text."""
        self._faults.setdefault(operation, []).extend([error] * times)

    def _raise_fault(self, operation: str) -> None:
        queued = self._faults.get(operation)
        if queued:
            raise queued.pop(0)("storage_injected")

    def seed_upload(
        self, object_key: str, provider_upload_id: str = "upload-1"
    ) -> None:
        self._uploads[provider_upload_id] = (object_key, "image/jpeg")

    def provider_upload_ids_for(self, object_key: str) -> set[str]:
        return {
            provider_upload_id
            for provider_upload_id, (stored_key, _content_type) in self._uploads.items()
            if stored_key == object_key
        }

    async def create_multipart(self, object_key: str, content_type: str) -> str:
        self.create_call_count += 1
        self.operation_log.append(("create", object_key))
        self._raise_fault("create")
        self.create_started.set()
        await self.create_release.wait()
        if self.create_delay_seconds:
            await asyncio.sleep(self.create_delay_seconds)
        if self.fail_create_times:
            self.fail_create_times -= 1
            raise StorageTransient("storage_transient")
        upload_id = f"upload-{self._next_upload}"
        self._next_upload += 1
        self._uploads[upload_id] = (object_key, content_type)
        return upload_id

    async def sign_part(
        self,
        object_key: str,
        provider_upload_id: str,
        part_number: int,
        expires_seconds: int,
    ) -> str:
        self.sign_call_count += 1
        self.operation_log.append(("sign", object_key))
        self._raise_fault("sign")
        if self._uploads.get(provider_upload_id, (None, None))[0] != object_key:
            raise StorageNotFound("storage_not_found")
        return f"https://storage.invalid/{object_key}?part={part_number}&expires={expires_seconds}&X-Amz-Signature=fake-signature"

    async def sign_download(
        self,
        object_key: str,
        expires_seconds: int,
    ) -> str:
        self.download_sign_call_count += 1
        self.download_sign_calls.append(object_key)
        self._raise_fault("sign_download")
        return (
            f"https://storage.invalid/{object_key}?expires={expires_seconds}"
            "&X-Amz-Signature=fake-download-signature"
        )

    async def complete_multipart(
        self, object_key: str, provider_upload_id: str, parts: Sequence[MultipartPart]
    ) -> ObjectHead:
        self.complete_attempt_count += 1
        self.operation_log.append(("complete", object_key))
        self.active_complete_calls += 1
        self.max_concurrent_complete_calls = max(
            self.max_concurrent_complete_calls, self.active_complete_calls
        )
        try:
            self._raise_fault("complete")
            if (
                self._uploads.get(provider_upload_id, (None, None))[0] != object_key
                or not parts
            ):
                raise StorageNotFound("storage_not_found")
            if self.complete_delay_seconds:
                await asyncio.sleep(self.complete_delay_seconds)
            self.complete_started.set()
            await self.complete_release.wait()
            if self._uploads.get(provider_upload_id, (None, None))[0] != object_key:
                raise StorageNotFound("storage_not_found")
            self._uploads.pop(provider_upload_id)
            self.complete_call_count += 1
            head = ObjectHead(self.completed_size, "fake-etag", None)
            self._heads[object_key] = head
            return head
        finally:
            self.active_complete_calls -= 1

    async def head_object(self, object_key: str) -> ObjectHead:
        self.head_call_count += 1
        self.operation_log.append(("head", object_key))
        self._raise_fault("head")
        try:
            return self._heads[object_key]
        except KeyError:
            raise StorageNotFound("storage_not_found") from None

    async def abort_multipart(self, object_key: str, provider_upload_id: str) -> None:
        self.abort_call_count += 1
        self.operation_log.append(("abort", object_key))
        self._raise_fault("abort")
        self.abort_started.set()
        await self.abort_release.wait()
        if self.fail_abort_times:
            self.fail_abort_times -= 1
            raise StorageTransient("storage_transient")
        self._uploads.pop(provider_upload_id, None)

    async def delete_object(self, object_key: str) -> None:
        self.delete_call_count += 1
        self.delete_calls_by_key[object_key] = (
            self.delete_calls_by_key.get(object_key, 0) + 1
        )
        self.operation_log.append(("delete", object_key))
        self._raise_fault("delete")
        if self.fail_delete_times:
            self.fail_delete_times -= 1
            raise StorageTransient("storage_transient")
        self._heads.pop(object_key, None)
