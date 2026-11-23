from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.handoff.delivery_ledger import (
    ProductImageSetDeliveryLedger,
    PublicationReceiptState,
)
from photo_organizer.handoff.http_publisher import (
    ProductImageSetHttpPublisher,
    PublicationDisposition,
)
from photo_organizer.handoff.product_image_set import ProductImageSetReadyV1
from photo_organizer.models.product_image_set_delivery import ProductImageSetDelivery


@dataclass(frozen=True)
class DeliveryPublisherRun:
    delivered: int
    retryable_failure: int
    permanent_failure: int


class ProductImageSetDeliveryPublisher:
    """Publish one bounded batch from the durable image-set delivery ledger."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: ProductImageSetHttpPublisher,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._ledger = ProductImageSetDeliveryLedger(session_factory)

    async def publish_pending(self, *, limit: int = 100) -> DeliveryPublisherRun:
        if not 1 <= limit <= 1000:
            raise ValueError("delivery_publisher_limit_out_of_range")
        counts = {
            PublicationDisposition.DELIVERED: 0,
            PublicationDisposition.RETRYABLE_FAILURE: 0,
            PublicationDisposition.PERMANENT_FAILURE: 0,
        }
        for event, digest, attempt in await self._due_deliveries(limit=limit):
            receipt = await self._publisher.publish(event, attempt=attempt)
            await self._ledger.record_publication_receipt(
                event_id=event.event_id,
                canonical_sha256=digest,
                state=PublicationReceiptState(
                    status=receipt.disposition.value,
                    retry_after_seconds=receipt.retry_after_seconds,
                    error_code=receipt.error_code,
                ),
            )
            counts[receipt.disposition] += 1
        return DeliveryPublisherRun(
            delivered=counts[PublicationDisposition.DELIVERED],
            retryable_failure=counts[PublicationDisposition.RETRYABLE_FAILURE],
            permanent_failure=counts[PublicationDisposition.PERMANENT_FAILURE],
        )

    async def _due_deliveries(
        self, *, limit: int
    ) -> tuple[tuple[ProductImageSetReadyV1, str, int], ...]:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            deliveries = (
                await session.scalars(
                    select(ProductImageSetDelivery)
                    .where(
                        or_(
                            ProductImageSetDelivery.publication_status == "pending",
                            (
                                ProductImageSetDelivery.publication_status
                                == "retryable_failure"
                            )
                            & (
                                ProductImageSetDelivery.publication_next_attempt_at
                                <= now
                            ),
                        )
                    )
                    .order_by(
                        ProductImageSetDelivery.publication_next_attempt_at.asc().nullsfirst(),
                        ProductImageSetDelivery.created_at,
                        ProductImageSetDelivery.event_id,
                    )
                    .limit(limit)
                )
            ).all()
            return tuple(
                (
                    ProductImageSetReadyV1.model_validate(delivery.payload),
                    delivery.canonical_sha256,
                    delivery.publication_attempts + 1,
                )
                for delivery in deliveries
            )

