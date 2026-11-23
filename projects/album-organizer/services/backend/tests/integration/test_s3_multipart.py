import hashlib
import os
from uuid import uuid4

import boto3
import httpx
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.models.task import ImportTask
from photo_organizer.models.user import User
from photo_organizer.schemas.assets import AssetInitRequest, CompletedPart
from photo_organizer.services.upload_service import UploadService
from photo_organizer.storage.s3_storage import S3Storage

pytestmark = pytest.mark.integration


class CountingS3Storage(S3Storage):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.complete_calls = 0

    async def complete_multipart(self, *args, **kwargs):
        self.complete_calls += 1
        return await super().complete_multipart(*args, **kwargs)


@pytest.mark.asyncio
async def test_real_minio_multipart_upload_is_private_sized_and_idempotent(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A public object, incorrect head size, or duplicate completion must fail this flow."""
    endpoint = os.environ["MINIO_TEST_ENDPOINT"]
    bucket = os.environ["MINIO_TEST_BUCKET"]
    access_key = os.environ["MINIO_TEST_ACCESS_KEY"]
    secret_key = os.environ["MINIO_TEST_SECRET_KEY"]
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        client.create_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "BucketAlreadyOwnedByYou",
            "BucketAlreadyExists",
        }:
            raise
    storage = CountingS3Storage(
        endpoint_url=endpoint,
        region_name="us-east-1",
        bucket=bucket,
        access_key_id=access_key,
        secret_access_key=secret_key,
    )
    service = UploadService(auth_session_factory, storage)
    user = User(id=uuid4(), phone_e164="+8613800014001")
    task = ImportTask(id=uuid4(), user_id=user.id, name="Real MinIO")
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add(task)
        await session.commit()

    payload = os.urandom(17 * 1024 * 1024)
    initiated = await service.initiate(
        user.id,
        task.id,
        AssetInitRequest(
            local_asset_id="device:17m",
            filename="IMG.jpg",
            media_type="image/jpeg",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ),
    )
    assert initiated.provider_upload_id is not None
    chunks = (
        payload[: 6 * 1024 * 1024],
        payload[6 * 1024 * 1024 : 12 * 1024 * 1024],
        payload[12 * 1024 * 1024 :],
    )
    parts: list[CompletedPart] = []
    async with httpx.AsyncClient(timeout=60) as http:
        for part_number, chunk in enumerate(chunks, start=1):
            signed = await service.sign_part(user.id, initiated.asset.id, part_number)
            response = await http.put(signed.url, content=chunk)
            assert response.status_code == 200, response.text
            parts.append(
                CompletedPart(part_number=part_number, etag=response.headers["etag"])
            )

    first = await service.complete(user.id, initiated.asset.id, parts)
    second = await service.complete(user.id, initiated.asset.id, parts)
    head = await storage.head_object(
        f"{user.id}/{task.id}/{initiated.asset.id}/original"
    )
    anonymous = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        config=Config(signature_version=UNSIGNED, s3={"addressing_style": "path"}),
    )
    with pytest.raises(ClientError):
        anonymous.get_object(
            Bucket=bucket, Key=f"{user.id}/{task.id}/{initiated.asset.id}/original"
        )

    assert first.asset.id == second.asset.id
    assert storage.complete_calls == 1
    assert head.byte_size == 17 * 1024 * 1024
