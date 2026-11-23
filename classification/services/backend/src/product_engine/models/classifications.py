from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from product_engine.models.base import Base


class ProductClassificationVersion(Base):
    __tablename__ = "product_classification_versions"
    __table_args__ = (
        CheckConstraint(
            "review_status = 'human_confirmed'",
            name="ck_classification_versions_review_status",
        ),
        CheckConstraint(
            "price_status IN ('pending', 'human_confirmed')",
            name="ck_classification_versions_price_status",
        ),
        CheckConstraint(
            "(material_claim_basis IS NULL AND material_evidence_id IS NULL) OR "
            "(material_claim_basis = 'information_insufficient' "
            "AND material_evidence_id IS NULL) OR "
            "(material_claim_basis IN ('document_supported', 'merchant_declared') "
            "AND material_evidence_id IS NOT NULL)",
            name="ck_classification_versions_material_provenance",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_classification_versions_tenant_product_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "request_id",
            name="uq_classification_versions_tenant_request",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_classification_versions_tenant_product_content",
        ),
        Index(
            "ix_classification_versions_tenant_product_version",
            "tenant_id",
            "product_id",
            "version",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_material_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("material_packages.id"), nullable=False
    )
    base_material_package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    material_label: Mapped[str] = mapped_column(String(128), nullable=False)
    product_form: Mapped[str] = mapped_column(String(64), nullable=False)
    main_asset_id: Mapped[UUID] = mapped_column(ForeignKey("source_images.id"), nullable=False)
    visual_observations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    selling_point_candidates: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    inventory_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    price_status: Mapped[str] = mapped_column(String(24), nullable=False)
    price_candidate: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    service_rule_summaries: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_image_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False)
    material_evidence: Mapped[str] = mapped_column(String(32), nullable=False)
    material_claim_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    material_evidence_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("material_evidence_records.id"), nullable=True
    )
    review_source: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProductClassificationAsset(Base):
    __tablename__ = "product_classification_assets"
    __table_args__ = (
        UniqueConstraint(
            "classification_version_id",
            "source_image_id",
            name="uq_classification_assets_version_source",
        ),
        UniqueConstraint(
            "classification_version_id",
            "position",
            name="uq_classification_assets_version_position",
        ),
        Index(
            "ix_classification_assets_tenant_version_position",
            "tenant_id",
            "classification_version_id",
            "position",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    classification_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_classification_versions.id"), nullable=False
    )
    source_image_id: Mapped[UUID] = mapped_column(ForeignKey("source_images.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
