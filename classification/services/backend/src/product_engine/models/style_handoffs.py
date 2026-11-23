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


class StyleTransformAuthorizationRecord(Base):
    __tablename__ = "style_transform_authorizations"
    __table_args__ = (
        CheckConstraint(
            "scope = 'local_style_acceptance_only'",
            name="ck_style_authorizations_local_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "authorization_request_id",
            name="uq_style_authorizations_tenant_request",
        ),
        Index(
            "ix_style_authorizations_tenant_source_scope",
            "tenant_id",
            "classification_id",
            "source_image_set_sha256",
            "scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    authorization_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    classification_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_classification_versions.id"), nullable=False
    )
    source_image_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(48), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StyleEligibilityInputV2Record(Base):
    __tablename__ = "style_eligibility_inputs_v2"
    __table_args__ = (
        CheckConstraint(
            "status = 'eligibility_only'",
            name="ck_style_inputs_eligibility_only",
        ),
        CheckConstraint(
            "transform_authorization_status IN ('none', 'active', 'expired', 'revoked')",
            name="ck_style_inputs_authorization_status",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_style_inputs_tenant_product_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_style_inputs_tenant_product_content",
        ),
        Index(
            "ix_style_inputs_tenant_product_version",
            "tenant_id",
            "product_id",
            "version",
        ),
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
    transform_authorization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("style_transform_authorizations.id"), nullable=True
    )
    transform_authorization_status: Mapped[str] = mapped_column(String(16), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StyleWarehouseHandoffRecord(Base):
    __tablename__ = "style_warehouse_handoffs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared', 'pending', 'accepted', 'rejected')",
            name="ck_style_handoffs_status",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_style_handoffs_tenant_key"),
        UniqueConstraint(
            "tenant_id",
            "eligibility_input_id",
            name="uq_style_handoffs_tenant_input",
        ),
        Index(
            "ix_style_handoffs_tenant_status",
            "tenant_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    eligibility_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("style_eligibility_inputs_v2.id"), nullable=False
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


class StyleWarehouseDecisionReceiptRecord(Base):
    __tablename__ = "style_warehouse_decision_receipts"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('accepted', 'rejected')",
            name="ck_style_decisions_decision",
        ),
        CheckConstraint(
            "generation_job_id IS NULL",
            name="ck_style_decisions_generation_empty",
        ),
        CheckConstraint(
            "(decision = 'rejected' AND rejection_code IS NOT NULL) OR "
            "(decision = 'accepted' AND rejection_code IS NULL)",
            name="ck_style_decisions_rejection_code",
        ),
        UniqueConstraint("tenant_id", "handoff_id", name="uq_style_decisions_tenant_handoff"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_style_decisions_tenant_key"),
        Index(
            "ix_style_decisions_tenant_decided",
            "tenant_id",
            "decided_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    handoff_id: Mapped[UUID] = mapped_column(
        ForeignKey("style_warehouse_handoffs.id"), nullable=False
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
