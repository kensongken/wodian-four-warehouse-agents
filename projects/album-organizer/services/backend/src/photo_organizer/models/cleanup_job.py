from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.naming import conv

from photo_organizer.models.base import Base, UUIDTimestampMixin


class StorageCleanupJob(UUIDTimestampMixin, Base):
    """Durable, retryable storage cleanup work; never put provider IDs in logs."""

    __tablename__ = "storage_cleanup_jobs"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('abort_multipart', 'delete_object')",
            name="storage_cleanup_jobs_operation_allowed",
        ),
        CheckConstraint(
            "(operation = 'abort_multipart' AND provider_upload_id IS NOT NULL) "
            "OR (operation = 'delete_object' AND provider_upload_id IS NULL)",
            name=conv("ck_storage_cleanup_jobs_storage_cleanup_jobs_provider_i_7f62"),
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'dead')",
            name="storage_cleanup_jobs_status_allowed",
        ),
        CheckConstraint(
            "attempts >= 0",
            name=conv("ck_storage_cleanup_jobs_storage_cleanup_jobs_attempts_n_f2ed"),
        ),
        CheckConstraint(
            "claim_generation >= 0",
            name=conv("ck_storage_cleanup_jobs_storage_cleanup_jobs_generation_a180"),
        ),
        CheckConstraint(
            "(status = 'processing' AND claim_id IS NOT NULL AND claimed_at IS NOT NULL "
            "AND terminal_code IS NULL) OR "
            "(status = 'pending' AND claim_id IS NULL AND claimed_at IS NULL "
            "AND terminal_code IS NULL) OR "
            "(status = 'dead' AND claim_id IS NULL AND claimed_at IS NULL "
            "AND terminal_code IS NOT NULL)",
            name=conv("ck_storage_cleanup_jobs_storage_cleanup_jobs_claim_stat_de7e"),
        ),
        ForeignKeyConstraint(
            ["user_id", "blob_id"],
            ["stored_blobs.user_id", "stored_blobs.id"],
            name="fk_cleanup_jobs_user_blob",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    blob_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    provider_upload_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    claim_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    terminal_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
