import hashlib
from io import BytesIO

import pytest

from product_engine.domain.errors import ObjectValidationFailed
from product_engine.storage.s3 import S3ObjectStorage


class FakeS3Client:
    def __init__(self, *, readback: bytes | None = None) -> None:
        self.readback = readback
        self.put_calls: list[dict[str, object]] = []
        self.objects: dict[str, tuple[bytes, str, str]] = {}

    def put_object(self, **values: object) -> dict[str, object]:
        self.put_calls.append(values)
        body = values["Body"]
        content_type = values["ContentType"]
        metadata = values["Metadata"]
        assert isinstance(body, bytes)
        assert isinstance(content_type, str)
        assert isinstance(metadata, dict)
        sha256 = metadata["sha256"]
        assert isinstance(sha256, str)
        self.objects[str(values["Key"])] = (body, content_type, sha256)
        return {"ETag": "verified"}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        body, content_type, sha256 = self.objects[Key]
        return {
            "ContentType": content_type,
            "ContentLength": len(body),
            "Metadata": {"sha256": sha256},
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        body, _, _ = self.objects[Key]
        return {"Body": BytesIO(self.readback if self.readback is not None else body)}

    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, object],
        ExpiresIn: int,
    ) -> str:
        del ClientMethod, Params, ExpiresIn
        return "https://storage.test/upload"


async def test_write_verified_uploads_metadata_and_reads_back_hash() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client, "private-products")
    content = b"employee-photo"
    digest = hashlib.sha256(content).hexdigest()

    stored = await storage.write_verified(
        "tenants/t/employee-handoffs/b/p/a",
        content,
        "image/jpeg",
        digest,
    )

    assert client.put_calls == [
        {
            "Bucket": "private-products",
            "Key": "tenants/t/employee-handoffs/b/p/a",
            "Body": content,
            "ContentType": "image/jpeg",
            "Metadata": {"sha256": digest},
        }
    ]
    assert stored.sha256 == digest
    assert stored.byte_size == len(content)


async def test_write_verified_rejects_input_hash_mismatch() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client, "private-products")
    with pytest.raises(ObjectValidationFailed, match="object_sha256_mismatch"):
        await storage.write_verified("safe/key", b"changed", "image/jpeg", "a" * 64)
    assert client.put_calls == []


async def test_write_verified_rejects_tampered_readback() -> None:
    client = FakeS3Client(readback=b"tampered")
    storage = S3ObjectStorage(client, "private-products")
    content = b"employee-photo"
    with pytest.raises(ObjectValidationFailed, match="object_sha256_mismatch"):
        await storage.write_verified(
            "safe/key",
            content,
            "image/jpeg",
            hashlib.sha256(content).hexdigest(),
        )

