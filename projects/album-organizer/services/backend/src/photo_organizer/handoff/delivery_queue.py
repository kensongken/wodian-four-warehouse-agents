from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.handoff.delivery_ledger import ProductImageSetDeliveryLedger
from photo_organizer.handoff.product_image_set import HandoffAdapterError
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.product_image_set_delivery import ProductImageSetDelivery


@dataclass(frozen=True)
class DeliveryQueueRun:
    created: int
    replayed: int
    blocked: dict[str, int]


class ProductImageSetDeliveryQueue:
    """A bounded, network-free worker that creates durable delivery records."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._ledger = ProductImageSetDeliveryLedger(session_factory)

    async def enqueue_pending(self, *, limit: int = 100) -> DeliveryQueueRun:
        if not 1 <= limit <= 1000:
            raise ValueError("delivery_queue_limit_out_of_range")
        created = 0
        replayed = 0
        blocked: Counter[str] = Counter()
        for classification_id in await self._pending_classification_ids(limit=limit):
            try:
                receipt = await self._ledger.enqueue_ready_for_classification(
                    source_classification_id=classification_id
                )
            except HandoffAdapterError as error:
                blocked[str(error)] += 1
                continue
            if receipt.disposition == "created":
                created += 1
            else:
                replayed += 1
        return DeliveryQueueRun(
            created=created,
            replayed=replayed,
            blocked=dict(sorted(blocked.items())),
        )

    async def _pending_classification_ids(self, *, limit: int) -> tuple[UUID, ...]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ProductClassification.id)
                .outerjoin(
                    ProductImageSetDelivery,
                    ProductImageSetDelivery.source_classification_id
                    == ProductClassification.id,
                )
                .where(
                    ProductClassification.status == "confirmed",
                    ProductImageSetDelivery.id.is_(None),
                )
                .order_by(
                    ProductClassification.confirmed_at,
                    ProductClassification.id,
                )
                .limit(limit)
            )
            return tuple(rows.all())
