from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class ClassificationReviewItem(UUIDTimestampMixin, Base):
    __tablename__ = "classification_review_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ("opc_id", "classification_id"),
            ("product_classifications.opc_id", "product_classifications.id"),
            name="classification_reviews_opc_classification_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "classification_id",
            name="classification_reviews_classification_key",
        ),
        CheckConstraint(
            "kind = 'classification'",
            name="kind_allowed",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved')",
            name="status_allowed",
        ),
        CheckConstraint(
            "(status = 'open' AND decision_payload IS NULL "
            "AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(status = 'resolved' AND decision_payload IS NOT NULL "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decision_state",
        ),
    )

    opc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    classification_id: Mapped[UUID] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="classification",
        server_default="classification",
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open"
    )
    decision_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
