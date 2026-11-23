from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class StoredBlob(UUIDTimestampMixin, Base):
    __tablename__ = "stored_blobs"
    __table_args__ = (
        Index(
            "stored_blobs_user_ready_sha256_size_key",
            "user_id",
            "sha256",
            "byte_size",
            unique=True,
            postgresql_where="is_ready",
        ),
        UniqueConstraint("user_id", "id", name="stored_blobs_user_id_id_key"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_lowercase_hex"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint(
            "initialization_generation >= 0",
            name="initialization_generation_nonnegative",
        ),
        CheckConstraint(
            "completion_generation >= 0", name="completion_generation_nonnegative"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_upload_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    provider_etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    provider_version_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    completed_parts: Mapped[str | None] = mapped_column(Text, nullable=True)
    initialization_claim_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    initialization_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    initialization_generation: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    completion_claim_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completion_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completion_generation: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    cleanup_pending: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    is_ready: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
