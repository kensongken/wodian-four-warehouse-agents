import asyncio
from collections.abc import Sequence
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from photo_organizer.storage.object_storage import (
    MultipartPart,
    ObjectHead,
    StorageNotFound,
    StoragePermanent,
    StorageTransient,
)


class S3Storage:
    """S3-compatible private multipart storage.  This adapter never logs URLs or keys."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        region_name: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        client: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    @staticmethod
    def _translate(error: Exception) -> Exception:
        if isinstance(error, ClientError):
            response = error.response
            code = str(response.get("Error", {}).get("Code", ""))
            status = int(
                response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0
            )
            if code == "NoSuchBucket":
                return StoragePermanent("storage_permanent")
            if (
                code
                in {"NoSuchKey", "NoSuchUpload", "NoSuchVersion", "NotFound", "404"}
                or status == 404
            ):
                return StorageNotFound("storage_not_found")
            if code in {
                "AccessDenied",
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "ExpiredToken",
                "InvalidToken",
                "InvalidPart",
                "InvalidPartOrder",
                "InvalidRequest",
                "InvalidArgument",
                "AuthorizationHeaderMalformed",
                "PermanentRedirect",
                "IncorrectEndpoint",
                "InvalidBucketName",
            }:
                return StoragePermanent("storage_permanent")
            if code in {
                "SlowDown",
                "Throttling",
                "ThrottlingException",
                "RequestTimeout",
                "Timeout",
                "429",
            }:
                return StorageTransient("storage_transient")
            if 400 <= status < 500 and status not in {408, 429}:
                return StoragePermanent("storage_permanent")
            if status >= 500:
                return StorageTransient("storage_transient")
        if isinstance(error, (ClientError, BotoCoreError)):
            return StorageTransient("storage_transient")
        return error

    async def create_multipart(self, object_key: str, content_type: str) -> str:
        try:
            response = await asyncio.to_thread(
                self._client.create_multipart_upload,
                Bucket=self._bucket,
                Key=object_key,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as error:
            raise self._translate(error) from None
        return str(response["UploadId"])

    async def sign_download(
        self,
        object_key: str,
        expires_seconds: int,
    ) -> str:
        try:
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                ClientMethod="get_object",
                Params={"Bucket": self._bucket, "Key": object_key},
                ExpiresIn=expires_seconds,
                HttpMethod="GET",
            )
        except (BotoCoreError, ClientError) as error:
            raise self._translate(error) from None

    async def sign_part(
        self,
        object_key: str,
        provider_upload_id: str,
        part_number: int,
        expires_seconds: int,
    ) -> str:
        try:
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                ClientMethod="upload_part",
                Params={
                    "Bucket": self._bucket,
                    "Key": object_key,
                    "UploadId": provider_upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=expires_seconds,
                HttpMethod="PUT",
            )
        except (BotoCoreError, ClientError) as error:
            raise self._translate(error) from None

    async def complete_multipart(
        self,
        object_key: str,
        provider_upload_id: str,
        parts: Sequence[MultipartPart],
    ) -> ObjectHead:
        try:
            await asyncio.to_thread(
                self._client.complete_multipart_upload,
                Bucket=self._bucket,
                Key=object_key,
                UploadId=provider_upload_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": part.part_number, "ETag": part.etag}
                        for part in parts
                    ]
                },
            )
        except (BotoCoreError, ClientError) as error:
            raise self._translate(error) from None
        return await self.head_object(object_key)

    async def head_object(self, object_key: str) -> ObjectHead:
        try:
            response = await asyncio.to_thread(
                self._client.head_object, Bucket=self._bucket, Key=object_key
            )
        except (BotoCoreError, ClientError) as error:
            raise self._translate(error) from None
        return ObjectHead(
            byte_size=int(response["ContentLength"]),
            etag=str(response.get("ETag", "")).strip('"'),
            version_id=response.get("VersionId"),
        )

    async def abort_multipart(self, object_key: str, provider_upload_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.abort_multipart_upload,
                Bucket=self._bucket,
                Key=object_key,
                UploadId=provider_upload_id,
            )
        except (BotoCoreError, ClientError) as error:
            raise self._translate(error) from None

    async def delete_object(self, object_key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=object_key
            )
        except (BotoCoreError, ClientError) as error:
            raise self._translate(error) from None
