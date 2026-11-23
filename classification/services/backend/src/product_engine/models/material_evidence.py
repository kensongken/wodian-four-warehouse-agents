from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from product_engine.models.base import Base


class MaterialEvidenceUploadRecord(Base):
    __tablename__ = "material_evidence_uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('staged', 'consumed')",
            name="ck_material_evidence_upload_status",
        ),
        CheckConstraint(
            "(status = 'staged' AND consumed_at IS NULL) OR "
            "(status = 'consumed' AND consumed_at IS NOT NULL)",
            name="ck_material_evidence_upload_consumption",
        ),
        UniqueConstraint(
            "tenant_id",
            "evidence_ref",
            name="uq_material_evidence_upload_tenant_ref",
        ),
        Index(
            "ix_material_evidence_uploads_tenant_product_status",
            "tenant_id",
            "product_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MaterialEvidenceRecord(Base):
    __tablename__ = "material_evidence_records"
    __table_args__ = (
        CheckConstraint(
            "claim_basis IN ('document_supported', 'merchant_declared')",
            name="ck_material_evidence_claim_basis",
        ),
        CheckConstraint(
            "evidence_kind IN ('inspection_certificate', 'purchase_record', "
            "'supplier_label', 'merchant_declaration', 'other_document')",
            name="ck_material_evidence_kind",
        ),
        CheckConstraint(
            "(claim_basis = 'merchant_declared' "
            "AND evidence_kind = 'merchant_declaration') OR "
            "(claim_basis = 'document_supported' "
            "AND evidence_kind <> 'merchant_declaration' "
            "AND evidence_sha256 IS NOT NULL)",
            name="ck_material_evidence_basis_kind",
        ),
        UniqueConstraint(
            "tenant_id",
            "request_id",
            name="uq_material_evidence_tenant_request",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_material_evidence_tenant_product_content",
        ),
        UniqueConstraint(
            "tenant_id",
            "evidence_ref",
            name="uq_material_evidence_tenant_ref",
        ),
        Index(
            "ix_material_evidence_tenant_product_created",
            "tenant_id",
            "product_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    base_classification_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_classification_versions.id"), nullable=False
    )
    material_label: Mapped[str] = mapped_column(String(128), nullable=False)
    claim_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    declared_by_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    declared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MaterialEvidenceRevocationRecord(Base):
    __tablename__ = "material_evidence_revocations"
    __table_args__ = (
        CheckConstraint(
            "reason_code IN ('merchant_withdrawn', 'document_invalidated', "
            "'wrong_product', 'other')",
            name="ck_material_evidence_revocation_reason",
        ),
        UniqueConstraint(
            "tenant_id",
            "evidence_id",
            name="uq_material_evidence_revocations_tenant_evidence",
        ),
        UniqueConstraint(
            "tenant_id",
            "request_id",
            name="uq_material_evidence_revocations_tenant_request",
        ),
        Index(
            "ix_material_evidence_revocations_tenant_evidence",
            "tenant_id",
            "evidence_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("material_evidence_records.id"), nullable=False
    )
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    revoked_by_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
