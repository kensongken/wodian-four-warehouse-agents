from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ObjectHead:
    byte_size: int
    etag: str
    version_id: str | None


@dataclass(frozen=True)
class MultipartPart:
    part_number: int
    etag: str


class StorageError(Exception):
    """Normalized storage-adapter failure; raw provider details never escape."""


class StorageNotFound(StorageError):
    """The requested object or multipart upload no longer exists."""


class StorageTransient(StorageError):
    """The provider was temporarily unavailable; durable work may be retried."""


class StoragePermanent(StorageError):
    """A credentials/configuration/request error requiring operator attention."""


class ObjectStorage(Protocol):
    async def create_multipart(self, object_key: str, content_type: str) -> str: ...

    async def sign_download(
        self,
        object_key: str,
        expires_seconds: int,
    ) -> str: ...

    async def sign_part(
        self,
        object_key: str,
        provider_upload_id: str,
        part_number: int,
        expires_seconds: int,
    ) -> str: ...

    async def complete_multipart(
        self,
        object_key: str,
        provider_upload_id: str,
        parts: Sequence[MultipartPart],
    ) -> ObjectHead: ...

    async def head_object(self, object_key: str) -> ObjectHead: ...

    async def abort_multipart(
        self, object_key: str, provider_upload_id: str
    ) -> None: ...

    async def delete_object(self, object_key: str) -> None: ...
