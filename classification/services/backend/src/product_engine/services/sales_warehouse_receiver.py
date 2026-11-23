import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from product_engine.contracts.sales_strategy_v2 import (
    SalesWarehouseDeliveryV1,
    SalesWarehouseReceiptV1,
    canonical_sales_strategy_v2_sha256,
)
from product_engine.domain.errors import InvalidSignature
from product_engine.security.signatures import verify_request_signature

Clock = Callable[[], int]
DELIVERY_PATH = "/internal/sales-warehouse/handoffs"
AUTOMATIC_PERMISSION_FIELDS = (
    "automatic_customer_contact_allowed",
    "automatic_publication_allowed",
    "automatic_wechat_send_allowed",
    "automatic_quote_allowed",
)


class SalesReceiverFailure(ValueError):
    pass


@dataclass(frozen=True)
class SignedSalesDelivery:
    body: bytes
    tenant_id: UUID
    timestamp: int
    signature: str


class SalesReceiptRepository(Protocol):
    async def accept(
        self, tenant_id: UUID, delivery: SalesWarehouseDeliveryV1
    ) -> SalesWarehouseReceiptV1: ...


class SalesWarehouseReceiver:
    def __init__(
        self,
        repository: SalesReceiptRepository,
        secret: bytes,
        *,
        now: Clock,
        tolerance_seconds: int = 300,
    ) -> None:
        self.repository = repository
        self.secret = secret
        self.now = now
        self.tolerance_seconds = tolerance_seconds

    async def receive(self, request: SignedSalesDelivery) -> SalesWarehouseReceiptV1:
        self._verify_signature(request)
        raw = self._raw_payload(request.body)
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise SalesReceiverFailure("sales_payload_hash_mismatch")
        if payload.get("tenant_id") != str(request.tenant_id):
            raise SalesReceiverFailure("sales_tenant_mismatch")
        if payload.get("contract_version") != "2-sales-strategy-input":
            raise SalesReceiverFailure("unsupported_sales_contract_version")
        if payload.get("status") != "preview_only":
            raise SalesReceiverFailure("sales_permission_boundary_violation")
        if any(payload.get(field) is not False for field in AUTOMATIC_PERMISSION_FIELDS):
            raise SalesReceiverFailure("sales_permission_boundary_violation")
        try:
            delivery = SalesWarehouseDeliveryV1.model_validate(raw)
        except ValidationError as error:
            messages = " ".join(item["msg"] for item in error.errors())
            code = (
                "warehouse_handoff_conflict"
                if "idempotency key" in messages
                else "sales_payload_hash_mismatch"
            )
            raise SalesReceiverFailure(code) from error
        if canonical_sales_strategy_v2_sha256(delivery.payload) != delivery.payload_sha256:
            raise SalesReceiverFailure("sales_payload_hash_mismatch")
        try:
            return await self.repository.accept(request.tenant_id, delivery)
        except SalesReceiverFailure:
            raise
        except ValueError as error:
            raise SalesReceiverFailure(str(error)) from error

    def _verify_signature(self, request: SignedSalesDelivery) -> None:
        try:
            verify_request_signature(
                secret=self.secret,
                timestamp=request.timestamp,
                now=self.now(),
                tolerance_seconds=self.tolerance_seconds,
                source_system="classification",
                tenant_id=request.tenant_id,
                method="POST",
                path=DELIVERY_PATH,
                body=request.body,
                supplied_signature=request.signature,
            )
        except InvalidSignature as error:
            raise SalesReceiverFailure("invalid_warehouse_signature") from error

    @staticmethod
    def _raw_payload(body: bytes) -> dict[str, object]:
        try:
            raw = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SalesReceiverFailure("sales_payload_hash_mismatch") from error
        if not isinstance(raw, dict):
            raise SalesReceiverFailure("sales_payload_hash_mismatch")
        return raw
