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


class SalesStrategyInputV2Record(Base):
    __tablename__ = "sales_strategy_inputs_v2"
    __table_args__ = (
        CheckConstraint(
            "status = 'preview_only'",
            name="ck_sales_strategy_inputs_v2_preview_only",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_sales_strategy_inputs_v2_tenant_product_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_sales_strategy_inputs_v2_tenant_product_content",
        ),
        UniqueConstraint(
            "tenant_id",
            "source_classification_id",
            name="uq_sales_strategy_inputs_v2_tenant_classification",
        ),
        Index(
            "ix_sales_strategy_inputs_v2_tenant_product_version",
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
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WarehouseHandoffRecord(Base):
    __tablename__ = "warehouse_handoffs"
    __table_args__ = (
        CheckConstraint(
            "target_warehouse = 'sales'",
            name="ck_warehouse_handoffs_sales",
        ),
        CheckConstraint(
            "status IN ('prepared', 'pending', 'accepted', 'rejected')",
            name="ck_warehouse_handoffs_status",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_warehouse_handoffs_tenant_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "strategy_input_id",
            "target_warehouse",
            name="uq_warehouse_handoffs_tenant_input_target",
        ),
        Index(
            "ix_warehouse_handoffs_tenant_status",
            "tenant_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    strategy_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("sales_strategy_inputs_v2.id"), nullable=False
    )
    target_warehouse: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    receiver_receipt_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    rejection_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prepared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SalesWarehouseReceiptRecord(Base):
    __tablename__ = "sales_warehouse_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "handoff_id",
            name="uq_sales_warehouse_receipts_tenant_handoff",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_sales_warehouse_receipts_tenant_key",
        ),
        Index(
            "ix_sales_warehouse_receipts_tenant_accepted",
            "tenant_id",
            "accepted_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    handoff_id: Mapped[UUID] = mapped_column(ForeignKey("warehouse_handoffs.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    accepted_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
