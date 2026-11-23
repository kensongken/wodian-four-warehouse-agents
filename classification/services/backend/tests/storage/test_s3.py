from io import BytesIO

import pytest

from product_engine.domain.errors import ObjectValidationFailed
from product_engine.storage.s3 import S3ObjectStorage


class FakeS3Client:
    def __init__(self) -> None:
        self.head_result = {
            "ContentType": "image/jpeg",
            "ContentLength": 820_000,
            "Metadata": {"sha256": "b" * 64},
        }
        self.presign_params = None
        self.head_params = None
        self.get_params = None

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        self.presign_params = (ClientMethod, Params, ExpiresIn)
        return "https://storage.test/upload"

    def head_object(self, *, Bucket, Key):
        self.head_params = (Bucket, Key)
        return self.head_result

    def get_object(self, *, Bucket, Key):
        self.get_params = (Bucket, Key)
        return {"Body": BytesIO(b"private-image")}


async def test_s3_adapter_presigns_private_put_and_inspects_metadata() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client, "private-products")
    object_key = "tenants/t/templates/T/v1/source.jpg"

    upload = await storage.create_upload(object_key, "image/jpeg", 20 * 1024 * 1024, "a" * 64)
    stored = await storage.inspect(object_key)

    assert upload.url == "https://storage.test/upload"
    assert upload.headers == {"Content-Type": "image/jpeg", "x-amz-meta-sha256": "a" * 64}
    assert client.presign_params == (
        "put_object",
        {
            "Bucket": "private-products",
            "Key": object_key,
            "ContentType": "image/jpeg",
            "Metadata": {"sha256": "a" * 64},
        },
        900,
    )
    assert client.head_params == ("private-products", object_key)
    assert stored.mime_type == "image/jpeg"
    assert stored.byte_size == 820_000
    assert stored.sha256 == "b" * 64


async def test_s3_adapter_rejects_object_without_sha256_metadata() -> None:
    client = FakeS3Client()
    client.head_result["Metadata"] = {}
    storage = S3ObjectStorage(client, "private-products")

    with pytest.raises(ObjectValidationFailed, match="object_metadata_missing"):
        await storage.inspect("tenants/t/templates/T/v1/source.jpg")


async def test_s3_adapter_reads_private_object_bytes() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(client, "private-products")

    content = await storage.read("tenants/t1/products/P1/main.png")

    assert content == b"private-image"
    assert client.get_params == (
        "private-products",
        "tenants/t1/products/P1/main.png",
    )
