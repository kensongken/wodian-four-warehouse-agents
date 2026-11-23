import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from product_engine.db import Database
from product_engine.models.sales_handoffs_v3 import SalesWarehouseReceiptV3Record
from product_engine.models.style_handoffs_v3 import StyleWarehouseDecisionReceiptV3Record
from product_engine.repositories.sales_handoffs_v3 import SalesHandoffRepositoryV3
from product_engine.repositories.style_handoffs_v3 import StyleHandoffRepositoryV3
from product_engine.security.signatures import sign_request
from product_engine.services.sales_strategy_v3 import SalesPreviewPreparerV3
from product_engine.services.style_eligibility_v3 import StyleEligibilityPreparerV3
from product_engine.services.style_warehouse_receiver_v3 import (
    DELIVERY_PATH_V3,
    SignedStyleDeliveryV3,
    StyleWarehouseReceiverV3,
)

Clock = Callable[[], int]


class RuntimeWarehouseV3Failure(ValueError):
    pass


class RuntimeWarehouseV3Service:
    def __init__(
        self, *, database: Database, tenant_id: UUID, style_secret: bytes, now: Clock
    ) -> None:
        self.database = database
        self.tenant_id = tenant_id
        self.style_secret = style_secret
        self.now = now

    async def prepare_sales(self, classification_id: UUID) -> object:
        try:
            async with self.database.session() as session:
                return await SalesPreviewPreparerV3(SalesHandoffRepositoryV3(session)).prepare(
                    self.tenant_id, classification_id
                )
        except ValueError as error:
            raise RuntimeWarehouseV3Failure(str(error)) from error

    async def deliver_sales(self, handoff_id: UUID) -> object:
        try:
            async with self.database.session() as session:
                repository = SalesHandoffRepositoryV3(session)
                delivery = await repository.mark_pending_and_build_delivery(
                    self.tenant_id, handoff_id
                )
                return await repository.accept_delivery(self.tenant_id, delivery)
        except ValueError as error:
            raise RuntimeWarehouseV3Failure(str(error)) from error

    async def prepare_style(self, classification_id: UUID) -> object:
        try:
            async with self.database.session() as session:
                return await StyleEligibilityPreparerV3(StyleHandoffRepositoryV3(session)).prepare(
                    self.tenant_id, classification_id
                )
        except ValueError as error:
            raise RuntimeWarehouseV3Failure(str(error)) from error

    async def evaluate_style(self, handoff_id: UUID) -> object:
        try:
            async with self.database.session() as session:
                delivery = await StyleHandoffRepositoryV3(session).mark_pending_and_build_delivery(
                    self.tenant_id, handoff_id
                )
            body = json.dumps(
                delivery.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            timestamp = self.now()
            signed = SignedStyleDeliveryV3(
                body=body,
                tenant_id=self.tenant_id,
                timestamp=timestamp,
                signature=sign_request(
                    self.style_secret,
                    timestamp,
                    "classification",
                    self.tenant_id,
                    "POST",
                    DELIVERY_PATH_V3,
                    body,
                ),
            )
            async with self.database.session() as session:
                return await StyleWarehouseReceiverV3(
                    StyleHandoffRepositoryV3(session),
                    self.style_secret,
                    now_seconds=self.now,
                    now_datetime=lambda: datetime.now(UTC),
                ).receive(signed)
        except ValueError as error:
            raise RuntimeWarehouseV3Failure(str(error)) from error

    async def read_receipt(self, receipt_id: UUID) -> object:
        async with self.database.session() as session:
            sales = await session.scalar(
                select(SalesWarehouseReceiptV3Record).where(
                    SalesWarehouseReceiptV3Record.tenant_id == self.tenant_id,
                    SalesWarehouseReceiptV3Record.id == receipt_id,
                )
            )
            if sales is not None:
                return sales.payload
            style = await session.scalar(
                select(StyleWarehouseDecisionReceiptV3Record).where(
                    StyleWarehouseDecisionReceiptV3Record.tenant_id == self.tenant_id,
                    StyleWarehouseDecisionReceiptV3Record.id == receipt_id,
                )
            )
            if style is not None:
                return style.payload
        raise RuntimeWarehouseV3Failure("warehouse_v3_receipt_not_found")
