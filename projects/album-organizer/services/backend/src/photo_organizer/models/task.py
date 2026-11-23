from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.domain.enums import TaskStatus
from photo_organizer.models.base import Base, UUIDTimestampMixin


class ImportTask(UUIDTimestampMixin, Base):
    __tablename__ = "import_tasks"
    __table_args__ = (
        CheckConstraint("total_assets >= 0", name="total_assets_non_negative"),
        CheckConstraint("uploaded_assets >= 0", name="uploaded_assets_non_negative"),
        CheckConstraint("failed_assets >= 0", name="failed_assets_non_negative"),
        CheckConstraint(
            "unsupported_assets >= 0", name="unsupported_assets_non_negative"
        ),
        UniqueConstraint("user_id", "id", name="import_tasks_user_id_id_key"),
        UniqueConstraint("id", "opc_id", name="import_tasks_id_opc_id_key"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", deferrable=True, initially="DEFERRED"),
        nullable=False,
        index=True,
    )
    opc_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("opc_workspaces.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="task_status",
            values_callable=lambda statuses: [item.value for item in statuses],
        ),
        nullable=False,
        default=TaskStatus.WAITING_UPLOAD,
        server_default=TaskStatus.WAITING_UPLOAD.value,
    )
    total_assets: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    uploaded_assets: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_assets: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    unsupported_assets: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
