from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.domain.enums import AssetStatus
from photo_organizer.models.base import Base, UUIDTimestampMixin


class Asset(UUIDTimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "task_id",
            "local_asset_id",
            name="assets_user_task_local_asset_key",
        ),
        UniqueConstraint(
            "user_id",
            "task_id",
            "id",
            name="assets_user_task_id_key",
        ),
        CheckConstraint(
            "(status = 'skipped_unsupported' "
            "AND (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$') "
            "AND (byte_size IS NULL OR byte_size >= 0)) "
            "OR (status <> 'skipped_unsupported' AND sha256 IS NOT NULL "
            "AND byte_size IS NOT NULL AND sha256 ~ '^[0-9a-f]{64}$' AND byte_size > 0)",
            name="accepted_content_metadata",
        ),
        CheckConstraint(
            "(status = 'skipped_duplicate' AND canonical_asset_id IS NOT NULL "
            "AND canonical_asset_id <> id AND blob_id IS NULL) OR "
            "(status <> 'skipped_duplicate' AND canonical_asset_id IS NULL)",
            name="duplicate_receipt_linkage",
        ),
        ForeignKeyConstraint(
            ["user_id", "task_id"],
            ["import_tasks.user_id", "import_tasks.id"],
            name="fk_assets_user_id_task_id_import_tasks",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["user_id", "task_id", "canonical_asset_id"],
            ["assets.user_id", "assets.task_id", "assets.id"],
            name="fk_assets_user_task_canonical_asset_assets",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_assets_task_content_canonical",
            "user_id",
            "task_id",
            "sha256",
            "byte_size",
            postgresql_where=text(
                "canonical_asset_id IS NULL AND status IN "
                "('pending', 'uploading', 'uploaded', 'failed')"
            ),
        ),
        ForeignKeyConstraint(
            ["user_id", "blob_id"],
            ["stored_blobs.user_id", "stored_blobs.id"],
            name="fk_assets_user_id_blob_id_stored_blobs",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", deferrable=True, initially="DEFERRED"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    blob_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    canonical_asset_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    local_asset_id: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[AssetStatus] = mapped_column(
        Enum(
            AssetStatus,
            name="asset_status",
            values_callable=lambda statuses: [item.value for item in statuses],
        ),
        nullable=False,
        default=AssetStatus.PENDING,
        server_default=AssetStatus.PENDING.value,
    )
    unsupported_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
