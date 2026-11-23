import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import PurePath
from typing import Protocol
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.domain.errors import ObjectValidationFailed
from product_engine.models.material_evidence import MaterialEvidenceUploadRecord
from product_engine.storage.protocol import StoredObject

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
MIME_EXTENSIONS: Mapping[str, tuple[str, ...]] = {
    "application/pdf": ("pdf",),
    "image/jpeg": ("jpg", "jpeg"),
    "image/png": ("png",),
}


class MaterialEvidenceUploadFailure(ValueError):
    pass


@dataclass(frozen=True)
class StagedMaterialEvidenceUpload:
    upload_id: UUID
    tenant_id: UUID
    product_id: UUID
    evidence_ref: str
    object_key: str
    mime_type: str
    byte_size: int
    sha256: str
    status: str
    created_at: datetime


class EvidenceObjectStorage(Protocol):
    async def save(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        sha256: str,
    ) -> None: ...

    async def read(self, object_key: str) -> bytes: ...

    async def inspect(self, object_key: str) -> StoredObject: ...


class MaterialEvidenceUploadStore(Protocol):
    async def add(self, upload: StagedMaterialEvidenceUpload) -> None: ...

    async def get(
        self,
        tenant_id: UUID,
        product_id: UUID,
        evidence_ref: str,
    ) -> StagedMaterialEvidenceUpload | None: ...


class SqlMaterialEvidenceUploadStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, upload: StagedMaterialEvidenceUpload) -> None:
        self.session.add(
            MaterialEvidenceUploadRecord(
                id=upload.upload_id,
                tenant_id=upload.tenant_id,
                product_id=upload.product_id,
                evidence_ref=upload.evidence_ref,
                object_key=upload.object_key,
                mime_type=upload.mime_type,
                byte_size=upload.byte_size,
                sha256=upload.sha256,
                status=upload.status,
                consumed_at=None,
                created_at=upload.created_at,
            )
        )
        await self.session.flush()

    async def get(
        self,
        tenant_id: UUID,
        product_id: UUID,
        evidence_ref: str,
    ) -> StagedMaterialEvidenceUpload | None:
        record = (
            await self.session.execute(
                select(MaterialEvidenceUploadRecord).where(
                    MaterialEvidenceUploadRecord.tenant_id == tenant_id,
                    MaterialEvidenceUploadRecord.product_id == product_id,
                    MaterialEvidenceUploadRecord.evidence_ref == evidence_ref,
                )
            )
        ).scalar_one_or_none()
        if record is None:
            return None
        return StagedMaterialEvidenceUpload(
            upload_id=record.id,
            tenant_id=record.tenant_id,
            product_id=record.product_id,
            evidence_ref=record.evidence_ref,
            object_key=record.object_key,
            mime_type=record.mime_type,
            byte_size=record.byte_size,
            sha256=record.sha256,
            status=record.status,
            created_at=record.created_at,
        )


class MaterialEvidenceUploadService:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        objects: EvidenceObjectStorage,
        store: MaterialEvidenceUploadStore,
    ) -> None:
        self.tenant_id = tenant_id
        self.objects = objects
        self.store = store

    async def stage(
        self,
        product_id: UUID,
        filename: str,
        declared_mime: str,
        content: bytes,
    ) -> StagedMaterialEvidenceUpload:
        extension = self._validate(filename, declared_mime, content)
        upload_id = uuid4()
        evidence_ref = f"material-evidence:{uuid4()}"
        object_key = f"tenants/{self.tenant_id}/material-evidence/{upload_id}/source.{extension}"
        sha256 = hashlib.sha256(content).hexdigest()
        await self.objects.save(object_key, content, declared_mime, sha256)
        staged = StagedMaterialEvidenceUpload(
            upload_id=upload_id,
            tenant_id=self.tenant_id,
            product_id=product_id,
            evidence_ref=evidence_ref,
            object_key=object_key,
            mime_type=declared_mime,
            byte_size=len(content),
            sha256=sha256,
            status="staged",
            created_at=datetime.now(UTC),
        )
        await self.store.add(staged)
        return staged

    async def resolve_for_registration(
        self,
        product_id: UUID,
        evidence_ref: str,
    ) -> StagedMaterialEvidenceUpload:
        staged = await self.store.get(self.tenant_id, product_id, evidence_ref)
        if staged is None or staged.status != "staged":
            raise MaterialEvidenceUploadFailure("material_evidence_upload_not_staged")
        try:
            stored = await self.objects.inspect(staged.object_key)
            content = await self.objects.read(staged.object_key)
        except (KeyError, ObjectValidationFailed) as error:
            raise MaterialEvidenceUploadFailure("material_evidence_hash_mismatch") from error
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if (
            stored.sha256 != staged.sha256
            or actual_sha256 != staged.sha256
            or stored.mime_type != staged.mime_type
            or stored.byte_size != staged.byte_size
        ):
            raise MaterialEvidenceUploadFailure("material_evidence_hash_mismatch")
        self._validate(
            f"evidence.{MIME_EXTENSIONS[staged.mime_type][0]}",
            staged.mime_type,
            content,
        )
        return staged

    @staticmethod
    def _validate(filename: str, declared_mime: str, content: bytes) -> str:
        if (
            not filename
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 for character in filename)
            or PurePath(filename).name != filename
        ):
            raise MaterialEvidenceUploadFailure("unsafe_filename")
        if not content:
            raise MaterialEvidenceUploadFailure("unsupported_evidence_file")
        if len(content) > MAX_EVIDENCE_BYTES:
            raise MaterialEvidenceUploadFailure("material_evidence_file_too_large")
        allowed_extensions = MIME_EXTENSIONS.get(declared_mime)
        if allowed_extensions is None:
            raise MaterialEvidenceUploadFailure("unsupported_evidence_file")
        extension = PurePath(filename).suffix.casefold().removeprefix(".")
        if extension not in allowed_extensions:
            raise MaterialEvidenceUploadFailure("mime_type_mismatch")

        detected_mime = MaterialEvidenceUploadService._detected_mime(content)
        if detected_mime is not None and detected_mime != declared_mime:
            raise MaterialEvidenceUploadFailure("mime_type_mismatch")
        if detected_mime is None:
            raise MaterialEvidenceUploadFailure("unsupported_evidence_file")

        if declared_mime == "application/pdf":
            MaterialEvidenceUploadService._validate_pdf(content)
        else:
            MaterialEvidenceUploadService._validate_image(content, declared_mime)
        return extension

    @staticmethod
    def _detected_mime(content: bytes) -> str | None:
        if content.startswith(b"%PDF-"):
            return "application/pdf"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        return None

    @staticmethod
    def _validate_image(content: bytes, declared_mime: str) -> None:
        expected_format = "JPEG" if declared_mime == "image/jpeg" else "PNG"
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format != expected_format:
                    raise MaterialEvidenceUploadFailure("mime_type_mismatch")
                image.verify()
        except MaterialEvidenceUploadFailure:
            raise
        except (OSError, UnidentifiedImageError) as error:
            raise MaterialEvidenceUploadFailure("unsupported_evidence_file") from error

    @staticmethod
    def _validate_pdf(content: bytes) -> None:
        try:
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted or len(reader.pages) == 0:
                raise MaterialEvidenceUploadFailure("unsupported_evidence_file")
        except MaterialEvidenceUploadFailure:
            raise
        except (OSError, PdfReadError, ValueError) as error:
            raise MaterialEvidenceUploadFailure("unsupported_evidence_file") from error
