from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class OpcWorkspace(Base):
    __tablename__ = "opc_workspaces"
    __table_args__ = (
        CheckConstraint(
            "char_length(id) BETWEEN 3 AND 128 "
            "AND id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="id_allowed",
        ),
        CheckConstraint(
            "char_length(btrim(display_name)) BETWEEN 1 AND 128",
            name="display_name_not_blank",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OpcMembership(UUIDTimestampMixin, Base):
    __tablename__ = "opc_memberships"
    __table_args__ = (
        UniqueConstraint(
            "opc_id",
            "user_id",
            name="opc_memberships_opc_user_key",
        ),
        CheckConstraint(
            "role IN ('owner', 'reviewer')",
            name="role_allowed",
        ),
        Index("ix_opc_memberships_user_id", "user_id"),
    )

    opc_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("opc_workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
