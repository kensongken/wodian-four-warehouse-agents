import asyncio
import hashlib
from typing import Any, Protocol

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from product_engine.domain.errors import ObjectValidationFailed
from product_engine.storage.protocol import PresignedUpload, StoredObject


class S3Client(Protocol):
    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, Any],
        ExpiresIn: int,
    ) -> str: ...

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        Metadata: dict[str, str],
    ) -> dict[str, Any]: ...


class S3ObjectStorage:
    def __init__(self, client: S3Client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    async def create_upload(
        self,
        object_key: str,
        mime_type: str,
        max_bytes: int,
        sha256: str | None = None,
    ) -> PresignedUpload:
        del max_bytes
        expires_in_seconds = 900
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": object_key,
            "ContentType": mime_type,
        }
        headers = {"Content-Type": mime_type}
        if sha256 is not None:
            params["Metadata"] = {"sha256": sha256}
            headers["x-amz-meta-sha256"] = sha256
        url = await asyncio.to_thread(
            self.client.generate_presigned_url,
            "put_object",
            Params=params,
            ExpiresIn=expires_in_seconds,
        )
        return PresignedUpload(
            object_key=object_key,
            url=url,
            headers=headers,
            expires_in_seconds=expires_in_seconds,
        )

    async def inspect(self, object_key: str) -> StoredObject:
        try:
            result = await asyncio.to_thread(
                self.client.head_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except ClientError as error:
            raise ObjectValidationFailed("object_not_found") from error

        metadata = result.get("Metadata", {})
        sha256 = metadata.get("sha256")
        mime_type = result.get("ContentType")
        byte_size = result.get("ContentLength")
        if not isinstance(sha256, str) or not isinstance(mime_type, str):
            raise ObjectValidationFailed("object_metadata_missing")
        if not isinstance(byte_size, int):
            raise ObjectValidationFailed("object_size_missing")
        return StoredObject(object_key, mime_type, byte_size, sha256)

    async def write_verified(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        sha256: str,
    ) -> StoredObject:
        if hashlib.sha256(content).hexdigest() != sha256:
            raise ObjectValidationFailed("object_sha256_mismatch")
        try:
            await asyncio.to_thread(
                self.client.put_object,
                Bucket=self.bucket,
                Key=object_key,
                Body=content,
                ContentType=mime_type,
                Metadata={"sha256": sha256},
            )
        except ClientError as error:
            raise ObjectValidationFailed("object_write_failed") from error
        stored = await self.inspect(object_key)
        readback = await self.read(object_key)
        if (
            stored.sha256 != sha256
            or stored.byte_size != len(content)
            or hashlib.sha256(readback).hexdigest() != sha256
        ):
            raise ObjectValidationFailed("object_sha256_mismatch")
        return stored

    async def read(self, object_key: str) -> bytes:
        def download() -> bytes:
            try:
                result = self.client.get_object(Bucket=self.bucket, Key=object_key)
                body = result.get("Body")
                if body is None or not hasattr(body, "read"):
                    raise ObjectValidationFailed("object_body_missing")
                content = body.read()
                close = getattr(body, "close", None)
                if close is not None:
                    close()
                if not isinstance(content, bytes):
                    raise ObjectValidationFailed("object_body_missing")
                return content
            except ClientError as error:
                raise ObjectValidationFailed("object_not_found") from error

        return await asyncio.to_thread(download)
