import pytest
from botocore.exceptions import ClientError

from photo_organizer.storage.object_storage import (
    StorageNotFound,
    StoragePermanent,
    StorageTransient,
)
from photo_organizer.storage.s3_storage import S3Storage


class _PresignClient:
    def __init__(self) -> None:
        self.call: dict[str, object] | None = None

    def generate_presigned_url(self, **kwargs: object) -> str:
        self.call = kwargs
        return "https://storage.invalid/private?X-Amz-Signature=secret"


def _error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "signed-url=secret provider detail"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "CompleteMultipartUpload",
    )


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("NoSuchKey", 404, StorageNotFound),
        ("NoSuchUpload", 404, StorageNotFound),
        ("InvalidPart", 400, StoragePermanent),
        ("InvalidPartOrder", 400, StoragePermanent),
        ("AccessDenied", 403, StoragePermanent),
        ("ExpiredToken", 400, StoragePermanent),
        ("NoSuchBucket", 404, StoragePermanent),
        ("PermanentRedirect", 301, StoragePermanent),
        ("SlowDown", 503, StorageTransient),
        ("Throttling", 429, StorageTransient),
        ("RequestTimeout", 408, StorageTransient),
        ("InternalError", 500, StorageTransient),
        ("UnknownClientFailure", 400, StoragePermanent),
    ],
)
def test_s3_error_translation_uses_safe_classification(
    code: str, status: int, expected: type[Exception]
) -> None:
    """Provider messages are discarded in favor of stable storage machine codes."""
    translated = S3Storage._translate(_error(code, status))

    assert isinstance(translated, expected)
    assert str(translated) in {
        "storage_not_found",
        "storage_permanent",
        "storage_transient",
    }
    assert "secret" not in str(translated)


@pytest.mark.asyncio
async def test_sign_download_uses_private_get_with_exact_expiry() -> None:
    client = _PresignClient()
    storage = S3Storage(
        endpoint_url="https://storage.invalid",
        region_name="test-region",
        bucket="bucket",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
        client=client,
    )

    url = await storage.sign_download("evidence/opc/item.jpg", 300)

    assert url.endswith("X-Amz-Signature=secret")
    assert client.call == {
        "ClientMethod": "get_object",
        "Params": {
            "Bucket": "bucket",
            "Key": "evidence/opc/item.jpg",
        },
        "ExpiresIn": 300,
        "HttpMethod": "GET",
    }
