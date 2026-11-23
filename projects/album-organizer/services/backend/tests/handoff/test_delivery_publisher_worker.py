from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from photo_organizer.handoff.delivery_publisher import ProductImageSetDeliveryPublisher
from photo_organizer.handoff.http_publisher import (
    PublicationDisposition,
    PublicationReceipt,
)
from photo_organizer.handoff.product_image_set import ProductImageSetReadyV1

EVENT_ID = UUID("11111111-1111-4111-8111-111111111111")


@pytest.mark.asyncio
async def test_publisher_sends_due_delivery_and_records_receipt() -> None:
    event = ProductImageSetReadyV1.model_validate(
        {
            "schema_version": 1,
            "event_id": str(EVENT_ID),
            "occurred_at": "2026-09-07T00:00:00Z",
            "tenant_id": "22222222-2222-4222-8222-222222222222",
            "product_code": "SYNTHETIC-001",
            "source_task_id": "33333333-3333-4333-8333-333333333333",
            "category": "南红",
            "form": "手串",
            "classification_confidence": 0.8,
            "images": [
                {
                    "asset_id": "44444444-4444-4444-8444-444444444444",
                    "object_key": "tenants/22222222-2222-4222-8222-222222222222/products/SYNTHETIC-001/main.png",
                    "sha256": "a" * 64,
                    "mime_type": "image/png",
                    "kind": "original",
                    "role": "main_candidate",
                    "quality_confidence": 0.9,
                }
            ],
        }
    )
    worker = ProductImageSetDeliveryPublisher(AsyncMock(), AsyncMock())
    worker._due_deliveries = AsyncMock(return_value=((event, "b" * 64, 2),))  # type: ignore[method-assign]
    worker._publisher.publish = AsyncMock(  # type: ignore[attr-defined]
        return_value=PublicationReceipt(
            PublicationDisposition.DELIVERED, 202, None
        )
    )
    worker._ledger.record_publication_receipt = AsyncMock()  # type: ignore[method-assign]

    result = await worker.publish_pending(limit=10)

    assert result.delivered == 1
    assert result.retryable_failure == 0
    assert result.permanent_failure == 0
    worker._publisher.publish.assert_awaited_once_with(event, attempt=2)  # type: ignore[attr-defined]
    worker._ledger.record_publication_receipt.assert_awaited_once()


@pytest.mark.asyncio
async def test_publisher_validates_limit_before_reading_database() -> None:
    worker = ProductImageSetDeliveryPublisher(AsyncMock(), AsyncMock())
    with pytest.raises(ValueError, match="delivery_publisher_limit_out_of_range"):
        await worker.publish_pending(limit=0)
