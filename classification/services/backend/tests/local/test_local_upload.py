import pytest

from product_engine.repositories.local_templates import LocalTemplateRepository
from product_engine.storage.local import LocalObjectStorage


def build_service(tmp_path):
    from product_engine.services.local_upload import LocalUploadService

    storage = LocalObjectStorage(tmp_path / "objects")
    repository = LocalTemplateRepository(tmp_path / "templates.json")
    return LocalUploadService(storage, repository), storage, repository


async def test_upload_generates_all_template_metadata(tmp_path) -> None:
    from product_engine.services.local_upload import LOCAL_TENANT_ID

    service, storage, repository = build_service(tmp_path)
    content = b"\xff\xd8\xffsimple-jpeg"

    record = await service.upload("客户照片.jpg", "image/jpeg", content)

    assert record.tenant_id == LOCAL_TENANT_ID
    assert record.template_code.startswith("LOCAL-")
    assert record.version == 1
    assert record.category == "待识别"
    assert record.image_type == "main"
    assert record.mime_type == "image/jpeg"
    assert record.byte_size == len(content)
    assert record.object_key.endswith("/v1/source.jpg")
    assert (await storage.inspect(record.object_key)).sha256 == record.sha256
    assert await repository.exists_version(LOCAL_TENANT_ID, record.template_code, 1)


@pytest.mark.parametrize(
    ("filename", "declared_mime", "content", "error_code"),
    [
        ("empty.jpg", "image/jpeg", b"", "empty_file"),
        ("notes.txt", "text/plain", b"hello", "unsupported_image"),
        ("fake.jpg", "image/jpeg", b"not-a-jpeg", "unsupported_image"),
        (
            "wrong.png",
            "image/png",
            b"\xff\xd8\xffjpeg-content",
            "mime_type_mismatch",
        ),
    ],
)
async def test_upload_rejects_invalid_images(
    tmp_path,
    filename,
    declared_mime,
    content,
    error_code,
) -> None:
    from product_engine.services.local_upload import LocalUploadRejected

    service, _, _ = build_service(tmp_path)

    with pytest.raises(LocalUploadRejected, match=error_code):
        await service.upload(filename, declared_mime, content)


async def test_upload_rejects_more_than_twenty_mib(tmp_path) -> None:
    from product_engine.services.local_upload import (
        MAX_UPLOAD_BYTES,
        LocalUploadRejected,
    )

    service, _, _ = build_service(tmp_path)
    content = b"\xff\xd8\xff" + b"x" * (MAX_UPLOAD_BYTES - 2)

    with pytest.raises(LocalUploadRejected, match="file_too_large"):
        await service.upload("large.jpg", "image/jpeg", content)


@pytest.mark.parametrize(
    ("filename", "declared_mime", "content", "expected_mime"),
    [
        ("photo.jpg", "image/jpg", b"\xff\xd8\xffjpeg", "image/jpeg"),
        ("photo.heic", "image/heif", b"\x00\x00\x00\x18ftypheic", "image/heic"),
    ],
)
async def test_upload_accepts_common_browser_mime_aliases(
    tmp_path,
    filename,
    declared_mime,
    content,
    expected_mime,
) -> None:
    service, _, _ = build_service(tmp_path)

    record = await service.upload(filename, declared_mime, content)

    assert record.mime_type == expected_mime
