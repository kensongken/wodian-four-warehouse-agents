from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from photo_organizer.handoff.delivery_ledger import (
    DeliveryLedgerReceipt,
    ProductImageSetDeliveryLedger,
)
from photo_organizer.handoff.product_image_set import HandoffAdapterError

CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")
TENANT_BINDING_ID = UUID("11111111-1111-4111-8111-111111111111")
PRODUCT_BINDING_ID = UUID("22222222-2222-4222-8222-222222222222")
PREPARED_IMAGE_ID = UUID("33333333-3333-4333-8333-333333333333")


class _ScalarRows:
    def __init__(self, values: list[UUID]) -> None:
        self._values = values

    def all(self) -> list[UUID]:
        return self._values


@asynccontextmanager
async def _session_context(session: AsyncMock):
    yield session


@pytest.mark.asyncio
async def test_queue_ready_delivery_discovers_operator_authority_then_queues() -> None:
    classification = SimpleNamespace(
        id=CLASSIFICATION_ID,
        opc_id="synthetic-opc",
        album_product_id="synthetic-product",
    )
    product = SimpleNamespace(id=PRODUCT_BINDING_ID)
    tenant = SimpleNamespace(id=TENANT_BINDING_ID)
    session = AsyncMock()
    session.scalar.side_effect = [classification, product, tenant]
    session.scalars.return_value = _ScalarRows([PREPARED_IMAGE_ID])
    ledger = ProductImageSetDeliveryLedger(lambda: _session_context(session))
    receipt = DeliveryLedgerReceipt(
        disposition="created",
        event_id=CLASSIFICATION_ID,
        canonical_sha256="a" * 64,
        payload={},
    )
    ledger.record_from_authority = AsyncMock(return_value=receipt)  # type: ignore[method-assign]

    actual = await ledger.queue_ready_delivery(
        source_classification_id=CLASSIFICATION_ID
    )

    assert actual == receipt
    ledger.record_from_authority.assert_awaited_once_with(
        source_classification_id=CLASSIFICATION_ID,
        tenant_binding_id=TENANT_BINDING_ID,
        product_binding_id=PRODUCT_BINDING_ID,
        prepared_image_binding_ids=(PREPARED_IMAGE_ID,),
    )


@pytest.mark.asyncio
async def test_queue_ready_delivery_fails_closed_without_operator_product_binding() -> None:
    classification = SimpleNamespace(
        id=CLASSIFICATION_ID,
        opc_id="synthetic-opc",
        album_product_id="synthetic-product",
    )
    session = AsyncMock()
    session.scalar.side_effect = [classification, None]
    ledger = ProductImageSetDeliveryLedger(lambda: _session_context(session))
    ledger.record_from_authority = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(HandoffAdapterError, match="handoff_authority_not_found"):
        await ledger.queue_ready_delivery(source_classification_id=CLASSIFICATION_ID)

    ledger.record_from_authority.assert_not_awaited()
