from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.contracts.material_evidence import (
    MaterialClaimBasis,
    MaterialEvidenceInsufficientCommand,
    MaterialEvidenceInsufficientReceipt,
    MaterialEvidenceKind,
    MaterialEvidenceReceipt,
    MaterialEvidenceRegistrationCommand,
    MaterialEvidenceRevocationCommand,
    MaterialEvidenceRevocationReason,
    MaterialEvidenceRevocationReceipt,
)
from product_engine.db import Database
from product_engine.models.classifications import ProductClassificationVersion
from product_engine.models.material_evidence import (
    MaterialEvidenceRecord,
    MaterialEvidenceRevocationRecord,
)
from product_engine.models.products import Product
from product_engine.repositories.material_evidence import (
    MaterialEvidenceActorContext,
    MaterialEvidenceFailure,
    MaterialEvidenceRepository,
)
from product_engine.services.material_evidence import MaterialEvidenceService
from product_engine.services.material_evidence_uploads import (
    EvidenceObjectStorage,
    MaterialEvidenceUploadFailure,
    MaterialEvidenceUploadService,
    SqlMaterialEvidenceUploadStore,
)


class MaterialEvidenceRegistrationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    base_classification_id: UUID
    material_label: str = Field(min_length=1, max_length=128)
    claim_basis: MaterialClaimBasis
    evidence_kind: MaterialEvidenceKind
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=255)
    source_summary: str = Field(min_length=1, max_length=500)
    acknowledge_not_authentication: bool


class MaterialEvidenceInsufficientInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    base_classification_id: UUID


class MaterialEvidenceRevocationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    reason_code: MaterialEvidenceRevocationReason
    reason_summary: str = Field(min_length=1, max_length=500)


class MaterialEvidenceUploadReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_ref: str
    mime_type: Literal["application/pdf", "image/jpeg", "image/png"]
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["staged"]


class MaterialEvidenceProductView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    product_code: str
    classification_id: UUID
    classification_version: int = Field(ge=1)
    material_label: str
    claim_basis: str
    evidence_id: UUID | None
    evidence_kind: str | None
    evidence_content_sha256: str | None
    evidence_revoked: bool
    source_summary: str | None


class RuntimeMaterialEvidenceService:
    def __init__(
        self,
        *,
        database: Database,
        tenant_id: UUID,
        objects: EvidenceObjectStorage,
        actor: MaterialEvidenceActorContext,
    ) -> None:
        self.database = database
        self.tenant_id = tenant_id
        self.objects = objects
        self.actor = actor

    async def upload(
        self,
        product_code: str,
        filename: str,
        mime_type: str,
        content: bytes,
    ) -> MaterialEvidenceUploadReceipt:
        async with self.database.session() as session:
            product = await self._product(session, product_code)
            upload = await MaterialEvidenceUploadService(
                tenant_id=self.tenant_id,
                objects=self.objects,
                store=SqlMaterialEvidenceUploadStore(session),
            ).stage(product.id, filename, mime_type, content)
        return MaterialEvidenceUploadReceipt(
            evidence_ref=upload.evidence_ref,
            mime_type=cast(
                Literal["application/pdf", "image/jpeg", "image/png"],
                upload.mime_type,
            ),
            byte_size=upload.byte_size,
            sha256=upload.sha256,
            status="staged",
        )

    async def register(
        self,
        product_code: str,
        request: MaterialEvidenceRegistrationInput,
    ) -> MaterialEvidenceReceipt:
        async with self.database.session() as session:
            product = await self._product(session, product_code)
            evidence_ref = request.evidence_ref
            evidence_sha256: str | None = None
            if evidence_ref is not None:
                upload = await MaterialEvidenceUploadService(
                    tenant_id=self.tenant_id,
                    objects=self.objects,
                    store=SqlMaterialEvidenceUploadStore(session),
                ).resolve_for_registration(product.id, evidence_ref)
                evidence_sha256 = upload.sha256
            elif request.claim_basis == "document_supported":
                raise MaterialEvidenceUploadFailure("material_evidence_attachment_required")
            else:
                evidence_ref = f"material-declaration:{uuid4()}"

            command = MaterialEvidenceRegistrationCommand(
                request_id=request.request_id,
                product_code=product_code,
                base_classification_id=request.base_classification_id,
                material_label=request.material_label,
                claim_basis=request.claim_basis,
                evidence_kind=request.evidence_kind,
                evidence_ref=evidence_ref,
                evidence_sha256=evidence_sha256,
                source_summary=request.source_summary,
                acknowledge_not_authentication=request.acknowledge_not_authentication,
            )
            return await MaterialEvidenceService(
                MaterialEvidenceRepository(session, self.tenant_id),
                self.actor,
            ).register(command)

    async def keep_insufficient(
        self,
        product_code: str,
        request: MaterialEvidenceInsufficientInput,
    ) -> MaterialEvidenceInsufficientReceipt:
        async with self.database.session() as session:
            await self._product(session, product_code)
            return await MaterialEvidenceService(
                MaterialEvidenceRepository(session, self.tenant_id),
                self.actor,
            ).keep_insufficient(
                MaterialEvidenceInsufficientCommand(
                    request_id=request.request_id,
                    product_code=product_code,
                    base_classification_id=request.base_classification_id,
                )
            )

    async def revoke(
        self,
        evidence_id: UUID,
        request: MaterialEvidenceRevocationInput,
    ) -> MaterialEvidenceRevocationReceipt:
        async with self.database.session() as session:
            return await MaterialEvidenceService(
                MaterialEvidenceRepository(session, self.tenant_id),
                self.actor,
            ).revoke(
                MaterialEvidenceRevocationCommand(
                    request_id=request.request_id,
                    evidence_id=evidence_id,
                    reason_code=request.reason_code,
                    reason_summary=request.reason_summary,
                )
            )

    async def read(self, product_code: str) -> MaterialEvidenceProductView:
        async with self.database.session() as session:
            product = await self._product(session, product_code)
            classification = (
                await session.execute(
                    select(ProductClassificationVersion)
                    .where(
                        ProductClassificationVersion.tenant_id == self.tenant_id,
                        ProductClassificationVersion.product_id == product.id,
                    )
                    .order_by(ProductClassificationVersion.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if classification is None:
                raise MaterialEvidenceFailure("material_evidence_classification_not_found")

            evidence: MaterialEvidenceRecord | None = None
            revoked = False
            if classification.material_evidence_id is not None:
                evidence = (
                    await session.execute(
                        select(MaterialEvidenceRecord).where(
                            MaterialEvidenceRecord.tenant_id == self.tenant_id,
                            MaterialEvidenceRecord.id == classification.material_evidence_id,
                        )
                    )
                ).scalar_one_or_none()
                if evidence is not None:
                    revoked = (
                        await session.execute(
                            select(MaterialEvidenceRevocationRecord.id).where(
                                MaterialEvidenceRevocationRecord.tenant_id == self.tenant_id,
                                MaterialEvidenceRevocationRecord.evidence_id == evidence.id,
                            )
                        )
                    ).scalar_one_or_none() is not None

            claim_basis = classification.material_claim_basis
            if claim_basis is None:
                claim_basis = (
                    "information_insufficient"
                    if classification.material_evidence == "information_insufficient"
                    else "legacy_unscoped_confirmation"
                )
            return MaterialEvidenceProductView(
                product_code=product_code,
                classification_id=classification.id,
                classification_version=classification.version,
                material_label=classification.material_label,
                claim_basis=claim_basis,
                evidence_id=None if evidence is None else evidence.id,
                evidence_kind=None if evidence is None else evidence.evidence_kind,
                evidence_content_sha256=(None if evidence is None else evidence.content_sha256),
                evidence_revoked=revoked,
                source_summary=None if evidence is None else evidence.source_summary,
            )

    async def _product(self, session: AsyncSession, product_code: str) -> Product:
        product = (
            await session.execute(
                select(Product).where(
                    Product.tenant_id == self.tenant_id,
                    Product.product_code == product_code,
                )
            )
        ).scalar_one_or_none()
        if product is None:
            raise MaterialEvidenceFailure("material_evidence_product_not_found")
        return product
