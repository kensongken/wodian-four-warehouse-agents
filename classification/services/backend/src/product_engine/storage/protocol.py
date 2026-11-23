from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PresignedUpload:
    object_key: str
    url: str
    headers: dict[str, str]
    expires_in_seconds: int


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    mime_type: str
    byte_size: int
    sha256: str


class ObjectStorage(Protocol):
    async def create_upload(
        self,
        object_key: str,
        mime_type: str,
        max_bytes: int,
        sha256: str | None = None,
    ) -> PresignedUpload: ...

    async def inspect(self, object_key: str) -> StoredObject: ...
