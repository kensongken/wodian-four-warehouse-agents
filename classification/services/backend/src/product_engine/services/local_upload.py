import hashlib
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from product_engine.domain.errors import DomainError
from product_engine.services.template_registry import (
    TemplateRecord,
    TemplateRegistry,
    TemplateStore,
)
from product_engine.storage.protocol import ObjectStorage

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
LOCAL_TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
}
DECLARED_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/heif": "image/heic",
}


class LocalUploadRejected(DomainError):
    pass


class WritableObjectStorage(ObjectStorage, Protocol):
    async def save(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        sha256: str,
    ) -> None: ...


def detect_image_mime(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        brand = content[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
    return None


class LocalUploadService:
    def __init__(
        self,
        storage: WritableObjectStorage,
        templates: TemplateStore,
    ) -> None:
        self.storage = storage
        self.templates = templates

    async def upload(
        self,
        filename: str,
        declared_mime: str,
        content: bytes,
    ) -> TemplateRecord:
        del filename
        if not content:
            raise LocalUploadRejected("empty_file")
        if len(content) > MAX_UPLOAD_BYTES:
            raise LocalUploadRejected("file_too_large")

        mime_type = detect_image_mime(content)
        if mime_type is None:
            raise LocalUploadRejected("unsupported_image")
        normalized_declared_mime = DECLARED_MIME_ALIASES.get(
            declared_mime,
            declared_mime,
        )
        if normalized_declared_mime in MIME_EXTENSIONS and normalized_declared_mime != mime_type:
            raise LocalUploadRejected("mime_type_mismatch")
        if normalized_declared_mime and normalized_declared_mime not in {
            *MIME_EXTENSIONS,
            "application/octet-stream",
        }:
            raise LocalUploadRejected("unsupported_image")

        template_code = f"LOCAL-{datetime.now(UTC):%Y%m%d%H%M%S}-{uuid4().hex[:8].upper()}"
        version = 1
        sha256 = hashlib.sha256(content).hexdigest()
        extension = MIME_EXTENSIONS[mime_type]
        object_key = (
            f"tenants/{LOCAL_TENANT_ID}/templates/{template_code}/v{version}/source.{extension}"
        )
        await self.storage.save(object_key, content, mime_type, sha256)
        registry = TemplateRegistry(self.storage, self.templates)
        return await registry.finalize(
            tenant_id=LOCAL_TENANT_ID,
            template_code=template_code,
            version=version,
            category="待识别",
            image_type="main",
            object_key=object_key,
            mime_type=mime_type,
            byte_size=len(content),
            sha256=sha256,
        )
