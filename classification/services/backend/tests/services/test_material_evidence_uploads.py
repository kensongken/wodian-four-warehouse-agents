import hashlib
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image

from product_engine.services.material_evidence_uploads import (
    MaterialEvidenceUploadFailure,
    MaterialEvidenceUploadService,
    StagedMaterialEvidenceUpload,
)
from product_engine.storage.protocol import StoredObject

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
PRODUCT_ID = UUID("22222222-2222-4222-8222-222222222222")


class MemoryObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def save(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        sha256: str,
    ) -> None:
        assert hashlib.sha256(content).hexdigest() == sha256
        self.objects[object_key] = (content, mime_type)

    async def read(self, object_key: str) -> bytes:
        return self.objects[object_key][0]

    async def inspect(self, object_key: str) -> StoredObject:
        content, mime_type = self.objects[object_key]
        return StoredObject(
            object_key=object_key,
            mime_type=mime_type,
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def replace_for_test(self, object_key: str, content: bytes) -> None:
        _, mime_type = self.objects[object_key]
        self.objects[object_key] = (content, mime_type)


class MemoryUploadStore:
    def __init__(self) -> None:
        self.rows: dict[str, StagedMaterialEvidenceUpload] = {}

    async def add(self, upload: StagedMaterialEvidenceUpload) -> None:
        self.rows[upload.evidence_ref] = upload

    async def get(
        self,
        tenant_id: UUID,
        product_id: UUID,
        evidence_ref: str,
    ) -> StagedMaterialEvidenceUpload | None:
        value = self.rows.get(evidence_ref)
        if value is None or value.tenant_id != tenant_id or value.product_id != product_id:
            return None
        return value


def png_bytes(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color=color).save(output, format="PNG")
    return output.getvalue()


def build_service() -> tuple[
    MaterialEvidenceUploadService,
    MemoryObjectStorage,
]:
    objects = MemoryObjectStorage()
    return (
        MaterialEvidenceUploadService(
            tenant_id=TENANT_ID,
            objects=objects,
            store=MemoryUploadStore(),
        ),
        objects,
    )


@pytest.mark.parametrize(
    ("filename", "declared_mime", "content", "code"),
    [
        ("../certificate.pdf", "application/pdf", b"%PDF-1.7\n", "unsafe_filename"),
        ("certificate.pdf", "image/jpeg", b"%PDF-1.7\n", "mime_type_mismatch"),
        (
            "certificate.pdf",
            "application/pdf",
            b"not-a-pdf",
            "unsupported_evidence_file",
        ),
        (
            "broken.pdf",
            "application/pdf",
            b"%PDF-1.7\nbroken",
            "unsupported_evidence_file",
        ),
        ("label.jpg", "image/jpeg", b"\x89PNG\r\n\x1a\n", "mime_type_mismatch"),
    ],
)
async def test_upload_rejects_untrusted_file_metadata(
    filename: str,
    declared_mime: str,
    content: bytes,
    code: str,
) -> None:
    service, _ = build_service()

    with pytest.raises(MaterialEvidenceUploadFailure, match=code):
        await service.stage(PRODUCT_ID, filename, declared_mime, content)


async def test_registration_rejects_staged_object_hash_mismatch() -> None:
    service, objects = build_service()
    staged = await service.stage(
        PRODUCT_ID,
        "label.png",
        "image/png",
        png_bytes("red"),
    )
    objects.replace_for_test(staged.object_key, png_bytes("blue"))

    with pytest.raises(
        MaterialEvidenceUploadFailure,
        match="material_evidence_hash_mismatch",
    ):
        await service.resolve_for_registration(PRODUCT_ID, staged.evidence_ref)


async def test_valid_image_is_staged_under_generated_tenant_prefix() -> None:
    service, _ = build_service()
    staged = await service.stage(
        PRODUCT_ID,
        "label.png",
        "image/png",
        png_bytes("red"),
    )

    assert staged.evidence_ref.startswith("material-evidence:")
    assert staged.object_key.startswith(f"tenants/{TENANT_ID}/material-evidence/")
    assert staged.status == "staged"
