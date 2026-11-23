from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID, uuid4

from product_engine.domain.errors import (
    ObjectValidationFailed,
    TemplateVersionConflict,
)
from product_engine.storage.protocol import ObjectStorage, PresignedUpload

MAX_TEMPLATE_BYTES = 20 * 1024 * 1024
MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
}


@dataclass(frozen=True)
class TemplateRecord:
    id: UUID
    tenant_id: UUID
    template_code: str
    version: int
    category: str
    image_type: str
    object_key: str
    sha256: str
    mime_type: str
    byte_size: int
    status: str = "active"


class TemplateStore(Protocol):
    async def exists_version(
        self,
        tenant_id: UUID,
        template_code: str,
        version: int,
    ) -> bool: ...

    async def create(self, record: TemplateRecord) -> TemplateRecord: ...

    async def list_active(
        self,
        tenant_id: UUID,
        category: str,
        status: str,
    ) -> list[TemplateRecord]: ...


class TemplateRegistry:
    def __init__(self, storage: ObjectStorage, templates: TemplateStore) -> None:
        self.storage = storage
        self.templates = templates

    async def request_upload(
        self,
        *,
        tenant_id: UUID,
        template_code: str,
        version: int,
        mime_type: str,
        sha256: str,
    ) -> PresignedUpload:
        extension = MIME_EXTENSIONS[mime_type]
        object_key = f"tenants/{tenant_id}/templates/{template_code}/v{version}/source.{extension}"
        upload = await self.storage.create_upload(
            object_key,
            mime_type,
            MAX_TEMPLATE_BYTES,
            sha256,
        )
        return replace(
            upload,
            headers={**upload.headers, "x-amz-meta-sha256": sha256},
        )

    async def finalize(
        self,
        *,
        tenant_id: UUID,
        template_code: str,
        version: int,
        category: str,
        image_type: str,
        object_key: str,
        mime_type: str,
        byte_size: int,
        sha256: str,
    ) -> TemplateRecord:
        expected_prefix = f"tenants/{tenant_id}/templates/{template_code}/v{version}/"
        if not object_key.startswith(expected_prefix):
            raise ObjectValidationFailed("object_outside_tenant_prefix")
        if await self.templates.exists_version(tenant_id, template_code, version):
            raise TemplateVersionConflict("template_version_exists")

        stored = await self.storage.inspect(object_key)
        if (
            stored.mime_type != mime_type
            or stored.byte_size != byte_size
            or stored.sha256 != sha256
            or stored.byte_size > MAX_TEMPLATE_BYTES
        ):
            raise ObjectValidationFailed("stored_object_mismatch")

        record = TemplateRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            template_code=template_code,
            version=version,
            category=category,
            image_type=image_type,
            object_key=object_key,
            sha256=sha256,
            mime_type=mime_type,
            byte_size=byte_size,
        )
        return await self.templates.create(record)

    async def list_active(
        self,
        tenant_id: UUID,
        category: str,
        status: str,
    ) -> list[TemplateRecord]:
        return await self.templates.list_active(tenant_id, category, status)
