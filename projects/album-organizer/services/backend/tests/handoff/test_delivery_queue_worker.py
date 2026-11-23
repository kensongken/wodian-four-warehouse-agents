from __future__ import annotations

from unittest.mock import AsyncMock, call
from uuid import UUID

import pytest

from photo_organizer.handoff.delivery_ledger import DeliveryLedgerReceipt
from photo_organizer.handoff.delivery_queue import ProductImageSetDeliveryQueue
from photo_organizer.handoff.product_image_set import HandoffAdapterError

FIRST_ID = UUID("11111111-1111-4111-8111-111111111111")
SECOND_ID = UUID("22222222-2222-4222-8222-222222222222")


@pytest.mark.asyncio
async def test_queue_worker_enqueues_once_and_reports_safe_block_code() -> None:
    worker = ProductImageSetDeliveryQueue(AsyncMock())
    worker._pending_classification_ids = AsyncMock(return_value=(FIRST_ID, SECOND_ID))  # type: ignore[method-assign]
    worker._ledger.enqueue_ready_for_classification = AsyncMock(  # type: ignore[method-assign]
        side_effect=(
            DeliveryLedgerReceipt(
                disposition="created",
                event_id=FIRST_ID,
                canonical_sha256="a" * 64,
                payload={},
            ),
            HandoffAdapterError("prepared_image_set_mismatch"),
        )
    )

    result = await worker.enqueue_pending(limit=10)

    assert result.created == 1
    assert result.replayed == 0
    assert result.blocked == {"prepared_image_set_mismatch": 1}
    worker._ledger.enqueue_ready_for_classification.assert_has_awaits(  # type: ignore[attr-defined]
        [
            call(source_classification_id=FIRST_ID),
            call(source_classification_id=SECOND_ID),
        ]
    )
