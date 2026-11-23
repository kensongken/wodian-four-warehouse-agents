from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class OpcExternalTenantBindingRecord(UUIDTimestampMixin, Base):
    __tablename__ = "opc_external_tenant_bindings"
    __table_args__ = (
        UniqueConstraint("opc_id", name="opc_external_tenant_bindings_opc_key"),
        UniqueConstraint("tenant_id", name="opc_external_tenant_bindings_tenant_key"),
        CheckConstraint(
            "binding_source = 'operator_provisioned'",
            name="binding_source_allowed",
        ),
        CheckConstraint(
            "char_length(btrim(provisioned_by)) BETWEEN 3 AND 128",
            name="provisioned_by_allowed",
        ),
    )

    opc_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("opc_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[UUID] = mapped_column(nullable=False)
    binding_source: Mapped[str] = mapped_column(String(32), nullable=False)
    provisioned_by: Mapped[str] = mapped_column(String(128), nullable=False)


class ProductCodeBindingRecord(UUIDTimestampMixin, Base):
    __tablename__ = "product_code_bindings"
    __table_args__ = (
        UniqueConstraint(
            "opc_id",
            "album_product_id",
            name="product_code_bindings_product_key",
        ),
        UniqueConstraint(
            "opc_id",
            "product_code",
            name="product_code_bindings_code_key",
        ),
        CheckConstraint(
            "product_code ~ '^[A-Za-z0-9_-]{1,64}$'",
            name="product_code_allowed",
        ),
        CheckConstraint(
            "binding_source = 'operator_provisioned'",
            name="binding_source_allowed",
        ),
        CheckConstraint(
            "char_length(btrim(provisioned_by)) BETWEEN 3 AND 128",
            name="provisioned_by_allowed",
        ),
    )

    opc_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("opc_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    album_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_source: Mapped[str] = mapped_column(String(32), nullable=False)
    provisioned_by: Mapped[str] = mapped_column(String(128), nullable=False)


class PreparedImageHandoffRecord(UUIDTimestampMixin, Base):
    __tablename__ = "prepared_image_handoffs"
    __table_args__ = (
        UniqueConstraint(
            "source_classification_id",
            "asset_id",
            name="prepared_image_handoffs_classification_asset_key",
        ),
        UniqueConstraint(
            "object_key",
            name="prepared_image_handoffs_object_key_key",
        ),
        CheckConstraint(
            "role IN ('main_candidate', 'gallery', 'source_original')",
            name="role_allowed",
        ),
        CheckConstraint(
            "quality_confidence BETWEEN 0 AND 1",
            name="quality_confidence_allowed",
        ),
        CheckConstraint(
            "storage_status = 'ready'",
            name="storage_status_allowed",
        ),
        CheckConstraint(
            "char_length(btrim(prepared_by)) BETWEEN 3 AND 128",
            name="prepared_by_allowed",
        ),
    )

    product_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_code_bindings.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_classification_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_classifications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_id: Mapped[UUID] = mapped_column(nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    storage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    prepared_by: Mapped[str] = mapped_column(String(128), nullable=False)
