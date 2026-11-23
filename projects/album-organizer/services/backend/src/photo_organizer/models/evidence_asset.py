from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class OpcEvidenceAsset(UUIDTimestampMixin, Base):
    __tablename__ = "opc_evidence_assets"
    __table_args__ = (
        UniqueConstraint(
            "source_asset_id",
            name="opc_evidence_assets_source_asset_key",
        ),
        UniqueConstraint(
            "storage_ref",
            name="opc_evidence_assets_storage_ref_key",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="sha256_lowercase_hex",
        ),
        CheckConstraint(
            "byte_size BETWEEN 1 AND 104857600",
            name="byte_size_allowed",
        ),
        CheckConstraint(
            "media_type IN ('image/jpeg', 'image/png', 'image/heic', 'image/heif')",
            name="media_type_allowed",
        ),
        CheckConstraint(
            "storage_ref ~ '^evidence/[0-9a-f-]{36}/original$'",
            name="storage_ref_allowed",
        ),
        Index("ix_opc_evidence_assets_opc_id", "opc_id"),
    )

    opc_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("opc_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_blob_id: Mapped[UUID] = mapped_column(
        ForeignKey("stored_blobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    storage_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
