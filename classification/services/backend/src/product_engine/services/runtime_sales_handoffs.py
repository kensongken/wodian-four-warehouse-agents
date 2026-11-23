import json
from collections.abc import Callable
from uuid import UUID

from product_engine.db import Database
from product_engine.repositories.sales_handoffs import (
    PreparedSalesPreview,
    SalesHandoffRepository,
    SalesHandoffRepositoryFailure,
    SalesWarehouseReceiptRepository,
    StoredSalesHandoff,
    WarehouseHandoffView,
)
from product_engine.security.signatures import sign_request
from product_engine.services.sales_strategy_v2 import SalesPreviewPreparer
from product_engine.services.sales_warehouse_receiver import (
    DELIVERY_PATH,
    SalesReceiverFailure,
    SalesWarehouseReceiver,
    SignedSalesDelivery,
)

Clock = Callable[[], int]


class RuntimeSalesHandoffFailure(ValueError):
    pass


class RuntimeSalesHandoffService:
    def __init__(
        self,
        *,
        database: Database,
        tenant_id: UUID,
        secret: bytes,
        now: Clock,
    ) -> None:
        self.database = database
        self.tenant_id = tenant_id
        self.secret = secret
        self.now = now

    async def prepare(self, classification_id: UUID) -> PreparedSalesPreview:
        try:
            async with self.database.session() as session:
                return await SalesPreviewPreparer(SalesHandoffRepository(session)).prepare(
                    self.tenant_id, classification_id
                )
        except ValueError as error:
            raise RuntimeSalesHandoffFailure(str(error)) from error

    async def deliver(self, handoff_id: UUID) -> WarehouseHandoffView:
        current = await self.read(handoff_id)
        try:
            async with self.database.session() as session:
                delivery = await SalesHandoffRepository(session).mark_pending_and_build_delivery(
                    self.tenant_id, handoff_id
                )
            body = json.dumps(
                delivery.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            timestamp = self.now()
            signed = SignedSalesDelivery(
                body=body,
                tenant_id=self.tenant_id,
                timestamp=timestamp,
                signature=sign_request(
                    self.secret,
                    timestamp,
                    "classification",
                    self.tenant_id,
                    "POST",
                    DELIVERY_PATH,
                    body,
                ),
            )
            async with self.database.session() as session:
                receipt = await SalesWarehouseReceiver(
                    SalesWarehouseReceiptRepository(session),
                    self.secret,
                    now=self.now,
                ).receive(signed)
            async with self.database.session() as session:
                return await SalesHandoffRepository(session).accept_handoff(
                    self.tenant_id, handoff_id, receipt
                )
        except SalesReceiverFailure as error:
            if current.handoff.status != "accepted":
                async with self.database.session() as session:
                    await SalesHandoffRepository(session).reject_handoff(
                        self.tenant_id, handoff_id, str(error)
                    )
            raise RuntimeSalesHandoffFailure(str(error)) from error
        except SalesHandoffRepositoryFailure as error:
            raise RuntimeSalesHandoffFailure(str(error)) from error

    async def read(self, handoff_id: UUID) -> StoredSalesHandoff:
        try:
            async with self.database.session() as session:
                value = await SalesHandoffRepository(session).read_handoff(
                    self.tenant_id, handoff_id
                )
        except SalesHandoffRepositoryFailure as error:
            raise RuntimeSalesHandoffFailure(str(error)) from error
        if value is None:
            raise RuntimeSalesHandoffFailure("warehouse_handoff_not_found")
        return value
