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


class ProductClassification(UUIDTimestampMixin, Base):
    __tablename__ = "product_classifications"
    __table_args__ = (
        UniqueConstraint(
            "candidate_event_id",
            name="product_classifications_candidate_event_key",
        ),
        UniqueConstraint(
            "opc_id",
            "id",
            name="product_classifications_opc_id_key",
        ),
        UniqueConstraint(
            "opc_id",
            "album_product_id",
            "input_snapshot_version",
            "classification_version",
            name="product_classifications_snapshot_version_key",
        ),
        CheckConstraint(
            "status IN ('requires_review', 'confirmed')",
            name="status_allowed",
        ),
        CheckConstraint(
            "source_type IN ('first_sale_bead', 'legacy_material', 'redesign_output')",
            name="source_type_allowed",
        ),
        CheckConstraint(
            "(status = 'requires_review' AND confirmed_level_1 IS NULL "
            "AND confirmed_level_2 IS NULL AND confirmed_by IS NULL "
            "AND confirmed_at IS NULL) OR "
            "(status = 'confirmed' AND confirmed_level_1 IS NOT NULL "
            "AND confirmed_level_2 IS NOT NULL AND confirmed_by IS NOT NULL "
            "AND confirmed_at IS NOT NULL)",
            name="confirmation_state",
        ),
        Index(
            "ix_product_classifications_opc_confirmed_manifest",
            "opc_id",
            text("confirmed_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 'confirmed'"),
        ),
    )

    opc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    album_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    input_event_id: Mapped[UUID] = mapped_column(nullable=False)
    candidate_event_id: Mapped[UUID] = mapped_column(nullable=False)
    input_snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    classification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_name: Mapped[str] = mapped_column(String(128), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(128), nullable=False)
    candidates: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="requires_review",
        server_default="requires_review",
    )
    confirmed_level_1: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_level_2: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
