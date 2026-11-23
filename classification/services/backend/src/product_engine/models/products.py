from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from product_engine.models.base import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "product_code",
            name="uq_products_tenant_product_code",
        ),
        Index("ix_products_tenant_product_code", "tenant_id", "product_code"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    source_task_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    form: Mapped[str] = mapped_column(String(64), nullable=False)
    classification_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceImage(Base):
    __tablename__ = "source_images"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "upstream_asset_id",
            name="uq_source_images_tenant_upstream_asset",
        ),
        Index("ix_source_images_tenant_product_id", "tenant_id", "product_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    upstream_asset_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)


class MaterialPackage(Base):
    __tablename__ = "material_packages"
    __table_args__ = (
        CheckConstraint("status = 'source_ready'", name="ck_material_packages_status"),
        CheckConstraint(
            "content_status = 'pending'",
            name="ck_material_packages_content_status",
        ),
        CheckConstraint(
            "price_status = 'pending'",
            name="ck_material_packages_price_status",
        ),
        UniqueConstraint(
            "tenant_id",
            "published_event_id",
            name="uq_material_packages_tenant_published_event",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_material_packages_tenant_product_version",
        ),
        Index("ix_material_packages_tenant_product_id", "tenant_id", "product_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    content_status: Mapped[str] = mapped_column(String(24), nullable=False)
    price_status: Mapped[str] = mapped_column(String(24), nullable=False)
    published_event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MaterialPackageAsset(Base):
    __tablename__ = "material_package_assets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "package_id",
            "source_image_id",
            name="uq_material_package_assets_source",
        ),
        UniqueConstraint(
            "tenant_id",
            "package_id",
            "position",
            name="uq_material_package_assets_position",
        ),
        Index(
            "ix_material_package_assets_tenant_package_id",
            "tenant_id",
            "package_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    package_id: Mapped[UUID] = mapped_column(ForeignKey("material_packages.id"), nullable=False)
    source_image_id: Mapped[UUID] = mapped_column(ForeignKey("source_images.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class SalesStrategyInputRecord(Base):
    __tablename__ = "sales_strategy_inputs"
    __table_args__ = (
        CheckConstraint(
            "status = 'preview_only'",
            name="ck_sales_strategy_inputs_preview_only",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "version",
            name="uq_sales_strategy_inputs_tenant_product_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "content_sha256",
            name="uq_sales_strategy_inputs_tenant_product_content",
        ),
        Index(
            "ix_sales_strategy_inputs_tenant_product_version",
            "tenant_id",
            "product_id",
            "version",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    product_data_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_album_event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_material_package_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_image_set_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
