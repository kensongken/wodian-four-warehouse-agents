from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class ControlledUploadBinding(UUIDTimestampMixin, Base):
    __tablename__ = "controlled_upload_bindings"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            name="controlled_upload_bindings_candidate_id_key",
        ),
        ForeignKeyConstraint(
            ("task_id", "opc_id"),
            ("import_tasks.id", "import_tasks.opc_id"),
            name="fk_controlled_upload_bindings_task_id_opc_id_import_tasks",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("user_id", "task_id"),
            ("import_tasks.user_id", "import_tasks.id"),
            name="fk_controlled_upload_bindings_user_id_task_id_import_tasks",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "candidate_id ~ '^opc-controlled-upload-candidate:[0-9a-f]{64}$'",
            name="candidate_id_allowed",
        ),
        CheckConstraint(
            "source_readiness_id ~ '^opc-real-sku-pilot-readiness:[0-9a-f]{64}$'",
            name="source_readiness_id_allowed",
        ),
        CheckConstraint(
            "source_type = 'first_sale_bead'",
            name="source_type_allowed",
        ),
        CheckConstraint(
            "item_count BETWEEN 2 AND 4",
            name="item_count_allowed",
        ),
        CheckConstraint(
            "candidate_payload IS NULL OR jsonb_typeof(candidate_payload) = 'object'",
            name="candidate_payload_object",
        ),
        Index(
            "ix_controlled_upload_bindings_opc_id_task_id",
            "opc_id",
            "task_id",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_readiness_id: Mapped[str] = mapped_column(String(128), nullable=False)
    opc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    album_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
