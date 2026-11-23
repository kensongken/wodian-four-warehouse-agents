from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class ProductImageSetDelivery(UUIDTimestampMixin, Base):
    __tablename__ = "product_image_set_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            name="product_image_set_deliveries_event_key",
        ),
        UniqueConstraint(
            "source_classification_id",
            name="product_image_set_deliveries_classification_key",
        ),
        CheckConstraint(
            "canonical_sha256 ~ '^[0-9a-f]{64}$'",
            name="canonical_sha256_lowercase_hex",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(nullable=False)
    source_classification_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_classifications.id", ondelete="RESTRICT"), nullable=False
    )
    source_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("album_product_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("opc_external_tenant_bindings.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_code_bindings.id", ondelete="RESTRICT"), nullable=False
    )
    prepared_image_binding_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    publication_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    publication_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    publication_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    publication_last_error_code: Mapped[str | None] = mapped_column(String(64))
    publication_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class ProductImageSetDeliveryImage(UUIDTimestampMixin, Base):
    __tablename__ = "product_image_set_delivery_images"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "prepared_image_id",
            name="product_image_set_delivery_images_pair_key",
        ),
        UniqueConstraint(
            "prepared_image_id",
            name="product_image_set_delivery_images_prepared_key",
        ),
    )

    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("product_image_set_deliveries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    prepared_image_id: Mapped[UUID] = mapped_column(
        ForeignKey("prepared_image_handoffs.id", ondelete="RESTRICT"),
        nullable=False,
    )
