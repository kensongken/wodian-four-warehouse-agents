import asyncio
import hashlib
import json
from pathlib import Path, PurePosixPath

from product_engine.domain.errors import ObjectValidationFailed
from product_engine.storage.protocol import PresignedUpload, StoredObject


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _object_path(self, object_key: str) -> Path:
        relative = PurePosixPath(object_key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ObjectValidationFailed("unsafe_object_key")
        path = self.root.joinpath(*relative.parts).resolve()
        if not path.is_relative_to(self.root):
            raise ObjectValidationFailed("unsafe_object_key")
        return path

    @staticmethod
    def _metadata_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.metadata.json")

    async def create_upload(
        self,
        object_key: str,
        mime_type: str,
        max_bytes: int,
        sha256: str | None = None,
    ) -> PresignedUpload:
        self._object_path(object_key)
        headers = {"Content-Type": mime_type}
        if sha256 is not None:
            headers["x-amz-meta-sha256"] = sha256
        return PresignedUpload(
            object_key=object_key,
            url=f"local://{object_key}",
            headers=headers,
            expires_in_seconds=900,
        )

    async def save(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        sha256: str,
    ) -> None:
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != sha256:
            raise ObjectValidationFailed("object_sha256_mismatch")
        await asyncio.to_thread(
            self._save_sync,
            object_key,
            content,
            mime_type,
            sha256,
        )

    def _save_sync(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        sha256: str,
    ) -> None:
        path = self._object_path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        content_tmp = path.with_name(f".{path.name}.uploading")
        metadata_path = self._metadata_path(path)
        metadata_tmp = metadata_path.with_name(f".{metadata_path.name}.uploading")
        content_tmp.write_bytes(content)
        metadata_tmp.write_text(
            json.dumps(
                {
                    "mime_type": mime_type,
                    "sha256": sha256,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        content_tmp.replace(path)
        metadata_tmp.replace(metadata_path)

    async def inspect(self, object_key: str) -> StoredObject:
        return await asyncio.to_thread(self._inspect_sync, object_key)

    async def read(self, object_key: str) -> bytes:
        path = self._object_path(object_key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as error:
            raise ObjectValidationFailed("object_not_found") from error

    def _inspect_sync(self, object_key: str) -> StoredObject:
        path = self._object_path(object_key)
        metadata_path = self._metadata_path(path)
        try:
            content = path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            mime_type = metadata["mime_type"]
            expected_sha256 = metadata["sha256"]
        except (FileNotFoundError, KeyError, json.JSONDecodeError) as error:
            raise ObjectValidationFailed("object_not_found") from error
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ObjectValidationFailed("object_sha256_mismatch")
        return StoredObject(
            object_key=object_key,
            mime_type=mime_type,
            byte_size=len(content),
            sha256=actual_sha256,
        )
