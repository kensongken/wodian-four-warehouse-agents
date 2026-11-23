import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ValidationError

from product_engine.contracts.style_warehouse_v2 import (
    StyleWarehouseDecisionReceiptV1,
    StyleWarehouseDeliveryV1,
    canonical_style_eligibility_v2_sha256,
)
from product_engine.domain.errors import InvalidSignature
from product_engine.repositories.style_handoffs import StyleAuthoritativeGateState
from product_engine.security.signatures import verify_request_signature

Clock = Callable[[], int]
DateClock = Callable[[], datetime]
DELIVERY_PATH = "/internal/style-warehouse/handoffs"
AUTOMATIC_PERMISSION_FIELDS = (
    "automatic_generation_allowed",
    "paid_provider_allowed",
    "automatic_publication_allowed",
    "automatic_customer_send_allowed",
)


class StyleReceiverFailure(ValueError):
    pass


@dataclass(frozen=True)
class SignedStyleDelivery:
    body: bytes
    tenant_id: UUID
    timestamp: int
    signature: str


class StyleDecisionRepository(Protocol):
    async def authoritative_gate_state(
        self, tenant_id: UUID, delivery: StyleWarehouseDeliveryV1
    ) -> StyleAuthoritativeGateState: ...

    async def persist_decision(
        self,
        tenant_id: UUID,
        delivery: StyleWarehouseDeliveryV1,
        *,
        decision: Literal["accepted", "rejected"],
        rejection_code: str | None,
    ) -> StyleWarehouseDecisionReceiptV1: ...


class StyleWarehouseReceiver:
    def __init__(
        self,
        repository: StyleDecisionRepository,
        secret: bytes,
        *,
        now_seconds: Clock,
        now_datetime: DateClock | None = None,
        tolerance_seconds: int = 300,
    ) -> None:
        self.repository = repository
        self.secret = secret
        self.now_seconds = now_seconds
        self.now_datetime = now_datetime or (lambda: datetime.now(UTC))
        self.tolerance_seconds = tolerance_seconds

    async def receive(self, request: SignedStyleDelivery) -> StyleWarehouseDecisionReceiptV1:
        self._verify_signature(request)
        raw = self._raw_payload(request.body)
        payload_raw = raw.get("payload")
        if not isinstance(payload_raw, dict):
            raise StyleReceiverFailure("style_payload_hash_mismatch")
        if payload_raw.get("tenant_id") != str(request.tenant_id):
            raise StyleReceiverFailure("style_tenant_mismatch")
        if payload_raw.get("contract_version") != "2-style-eligibility-input":
            raise StyleReceiverFailure("unsupported_style_contract_version")
        delivery = self._parse_delivery(raw, payload_raw)
        if canonical_style_eligibility_v2_sha256(delivery.payload) != (delivery.payload_sha256):
            raise StyleReceiverFailure("style_payload_hash_mismatch")
        try:
            state = await self.repository.authoritative_gate_state(request.tenant_id, delivery)
            rejection_code = self._evaluate(delivery, state)
            return await self.repository.persist_decision(
                request.tenant_id,
                delivery,
                decision="rejected" if rejection_code else "accepted",
                rejection_code=rejection_code,
            )
        except StyleReceiverFailure:
            raise
        except ValueError as error:
            raise StyleReceiverFailure(str(error)) from error

    def _evaluate(
        self,
        delivery: StyleWarehouseDeliveryV1,
        state: StyleAuthoritativeGateState,
    ) -> str | None:
        payload = delivery.payload
        if (
            state.classification_id != payload.source_classification_id
            or state.classification_version != payload.source_classification_version
            or state.classification_sha256 != payload.source_classification_sha256
        ):
            return "classification_not_latest"
        if (
            state.material_evidence != "human_confirmed"
            or payload.material.evidence != "human_confirmed"
        ):
            return "classification_material_unconfirmed"
        authorization = state.authorization
        if payload.transform_authorization_id is None or authorization is None:
            return "style_transform_authorization_required"
        if authorization.revoked_at is not None:
            return "style_transform_authorization_revoked"
        if authorization.expires_at <= self.now_datetime():
            return "style_transform_authorization_expired"
        if (
            authorization.authorization_id != payload.transform_authorization_id
            or authorization.classification_id != payload.source_classification_id
            or authorization.scope != "local_style_acceptance_only"
        ):
            return "style_transform_authorization_required"
        if (
            state.source_image_set_sha256 != payload.source_image_set_sha256
            or authorization.source_image_set_sha256 != payload.source_image_set_sha256
        ):
            return "style_image_set_mismatch"
        if any(getattr(payload, field) is not False for field in AUTOMATIC_PERMISSION_FIELDS):
            return "style_permission_boundary_violation"
        return None

    def _parse_delivery(
        self, raw: dict[str, object], payload_raw: dict[str, object]
    ) -> StyleWarehouseDeliveryV1:
        permission_values = {
            field: payload_raw.get(field, False) for field in AUTOMATIC_PERMISSION_FIELDS
        }
        safe_payload = {**payload_raw}
        safe_payload.update({field: False for field in AUTOMATIC_PERMISSION_FIELDS})
        safe_raw = {**raw, "payload": safe_payload}
        try:
            delivery = StyleWarehouseDeliveryV1.model_validate(safe_raw)
        except ValidationError as error:
            messages = " ".join(item["msg"] for item in error.errors())
            code = (
                "style_handoff_conflict"
                if "idempotency key" in messages
                else "style_payload_hash_mismatch"
            )
            raise StyleReceiverFailure(code) from error
        if any(value is not False for value in permission_values.values()):
            payload = delivery.payload.model_copy(update=permission_values)
            delivery = delivery.model_copy(update={"payload": payload})
        return delivery

    def _verify_signature(self, request: SignedStyleDelivery) -> None:
        try:
            verify_request_signature(
                secret=self.secret,
                timestamp=request.timestamp,
                now=self.now_seconds(),
                tolerance_seconds=self.tolerance_seconds,
                source_system="classification",
                tenant_id=request.tenant_id,
                method="POST",
                path=DELIVERY_PATH,
                body=request.body,
                supplied_signature=request.signature,
            )
        except InvalidSignature as error:
            raise StyleReceiverFailure("invalid_style_warehouse_signature") from error

    @staticmethod
    def _raw_payload(body: bytes) -> dict[str, object]:
        try:
            raw = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StyleReceiverFailure("style_payload_hash_mismatch") from error
        if not isinstance(raw, dict):
            raise StyleReceiverFailure("style_payload_hash_mismatch")
        return raw
