from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class AlbumProductSnapshot(UUIDTimestampMixin, Base):
    __tablename__ = "album_product_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source_event_id",
            name="album_product_snapshots_source_event_key",
        ),
        UniqueConstraint(
            "opc_id",
            "album_product_id",
            "snapshot_version",
            name="album_product_snapshots_product_version_key",
        ),
        CheckConstraint(
            "snapshot_version >= 1",
            name="snapshot_version_positive",
        ),
        CheckConstraint(
            "source_type IN ('first_sale_bead', 'legacy_material', 'redesign_output')",
            name="source_type_allowed",
        ),
        CheckConstraint(
            "consent_basis = 'opc_owned_product_material'",
            name="consent_basis_allowed",
        ),
        CheckConstraint(
            "evidence_resolution_mode IN ('legacy_direct', 'registry_required')",
            name="evidence_resolution_mode_allowed",
        ),
        Index(
            "ix_album_product_snapshots_opc_confirmation",
            "opc_id",
            text("group_confirmed_at DESC"),
            text("id DESC"),
        ),
    )

    source_event_id: Mapped[UUID] = mapped_column(nullable=False)
    opc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    album_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_import_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    group_confirmed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    group_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consent_basis: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_resolution_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="registry_required",
        server_default="registry_required",
    )
    allowed_consumers: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    images: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    source_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
