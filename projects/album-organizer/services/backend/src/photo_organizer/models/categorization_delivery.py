from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class CategorizationDelivery(UUIDTimestampMixin, Base):
    __tablename__ = "categorization_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "consumer_name",
            "source_event_id",
            name="categorization_deliveries_consumer_event_key",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'retryable_failed', "
            "'completed', 'terminal_failed')",
            name="status_allowed",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND claim_generation >= 0",
            name="counters_nonnegative",
        ),
        CheckConstraint(
            "(status = 'processing' AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claim_token IS NULL "
            "AND claim_expires_at IS NULL)",
            name="claim_state",
        ),
    )

    consumer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_event_id: Mapped[UUID] = mapped_column(nullable=False)
    source_stream_id: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claim_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claim_token: Mapped[UUID | None] = mapped_column(nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
