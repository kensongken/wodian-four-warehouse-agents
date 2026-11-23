import pytest

from photo_organizer.handoff.delivery_ledger import ProductImageSetDeliveryLedger


@pytest.mark.asyncio
async def test_ledger_exposes_receipt_persistence_api() -> None:
    assert hasattr(ProductImageSetDeliveryLedger, "record_publication_receipt")
