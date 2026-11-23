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


class StyleEligibilityInputV3Record(Base):
    __tablename__ = "style_eligibility_inputs_v3"
    __table_args__ = (
        CheckConstraint("status = 'eligibility_only'", name="ck_style_inputs_v3_eligibility_only"),
        UniqueConstraint(
            "tenant_id", "product_id", "version", name="uq_style_inputs_v3_tenant_product_version"
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_style_inputs_v3_tenant_product_content",
        ),
        UniqueConstraint(
            "tenant_id", "source_classification_id", name="uq_style_inputs_v3_tenant_classification"
        ),
        Index("ix_style_inputs_v3_tenant_product_version", "tenant_id", "product_id", "version"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_material_package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_classification_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_classification_versions.id"), nullable=False
    )
    source_classification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_classification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_image_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StyleWarehouseHandoffV3Record(Base):
    __tablename__ = "style_warehouse_handoffs_v3"
    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared', 'pending', 'accepted', 'rejected')",
            name="ck_style_handoffs_v3_status",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_style_handoffs_v3_tenant_key"),
        UniqueConstraint(
            "tenant_id", "eligibility_input_id", name="uq_style_handoffs_v3_tenant_input"
        ),
        Index("ix_style_handoffs_v3_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    eligibility_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("style_eligibility_inputs_v3.id"), nullable=False
    )
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_receipt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prepared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StyleWarehouseDecisionReceiptV3Record(Base):
    __tablename__ = "style_warehouse_decision_receipts_v3"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('accepted', 'rejected')", name="ck_style_decisions_v3_decision"
        ),
        CheckConstraint("generation_job_id IS NULL", name="ck_style_decisions_v3_generation_empty"),
        CheckConstraint(
            "(decision = 'rejected' AND rejection_code IS NOT NULL) OR "
            "(decision = 'accepted' AND rejection_code IS NULL)",
            name="ck_style_decisions_v3_rejection_code",
        ),
        UniqueConstraint("tenant_id", "handoff_id", name="uq_style_decisions_v3_tenant_handoff"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_style_decisions_v3_tenant_key"),
        Index("ix_style_decisions_v3_tenant_decided", "tenant_id", "decided_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    handoff_id: Mapped[UUID] = mapped_column(
        ForeignKey("style_warehouse_handoffs_v3.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    checked_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation_job_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
