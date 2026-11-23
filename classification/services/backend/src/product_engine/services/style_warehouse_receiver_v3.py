import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ValidationError

from product_engine.contracts.style_warehouse_v3 import (
    StyleWarehouseDecisionReceiptV2,
    StyleWarehouseDeliveryV2,
    canonical_style_eligibility_v3_sha256,
)
from product_engine.domain.errors import InvalidSignature
from product_engine.repositories.style_handoffs_v3 import StyleAuthoritativeGateStateV3
from product_engine.security.signatures import verify_request_signature

Clock = Callable[[], int]
DateClock = Callable[[], datetime]
DELIVERY_PATH_V3 = "/internal/style-warehouse/v3/handoffs"
AUTOMATIC_PERMISSION_FIELDS = (
    "automatic_generation_allowed",
    "paid_provider_allowed",
    "automatic_publication_allowed",
    "automatic_customer_send_allowed",
)


class StyleReceiverFailureV3(ValueError):
    pass


@dataclass(frozen=True)
class SignedStyleDeliveryV3:
    body: bytes
    tenant_id: UUID
    timestamp: int
    signature: str


class StyleDecisionRepositoryV3(Protocol):
    async def authoritative_gate_state(
        self, tenant_id: UUID, delivery: StyleWarehouseDeliveryV2
    ) -> StyleAuthoritativeGateStateV3: ...
    async def persist_decision(
        self,
        tenant_id: UUID,
        delivery: StyleWarehouseDeliveryV2,
        *,
        decision: Literal["accepted", "rejected"],
        rejection_code: str | None,
    ) -> StyleWarehouseDecisionReceiptV2: ...


class StyleWarehouseReceiverV3:
    def __init__(
        self,
        repository: StyleDecisionRepositoryV3,
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

    async def receive(self, request: SignedStyleDeliveryV3) -> StyleWarehouseDecisionReceiptV2:
        self._verify_signature(request)
        try:
            raw = json.loads(request.body)
            payload_raw = raw["payload"]
            permission_values = {
                field: payload_raw.get(field, False) for field in AUTOMATIC_PERMISSION_FIELDS
            }
            safe_raw = {
                **raw,
                "payload": {
                    **payload_raw,
                    **{field: False for field in AUTOMATIC_PERMISSION_FIELDS},
                },
            }
            delivery = StyleWarehouseDeliveryV2.model_validate(safe_raw)
        except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as error:
            raise StyleReceiverFailureV3("style_payload_hash_mismatch") from error
        if delivery.payload.tenant_id != request.tenant_id:
            raise StyleReceiverFailureV3("style_tenant_mismatch")
        if canonical_style_eligibility_v3_sha256(delivery.payload) != delivery.payload_sha256:
            raise StyleReceiverFailureV3("style_payload_hash_mismatch")
        state = await self.repository.authoritative_gate_state(request.tenant_id, delivery)
        rejection = self._evaluate(delivery, state, permission_values)
        return await self.repository.persist_decision(
            request.tenant_id,
            delivery,
            decision="rejected" if rejection else "accepted",
            rejection_code=rejection,
        )

    def _evaluate(
        self,
        delivery: StyleWarehouseDeliveryV2,
        state: StyleAuthoritativeGateStateV3,
        permission_values: dict[str, object],
    ) -> str | None:
        payload = delivery.payload
        if (
            state.classification_id != payload.source_classification_id
            or state.classification_version != payload.source_classification_version
            or state.classification_sha256 != payload.source_classification_sha256
        ):
            return "classification_not_latest"
        if (
            state.material_claim_basis == "information_insufficient"
            or payload.material.claim_basis == "information_insufficient"
        ):
            return "classification_material_unconfirmed"
        if (
            state.material_claim_basis not in {"document_supported", "merchant_declared"}
            or state.material_evidence_id is None
        ):
            return "material_evidence_provenance_missing"
        if (
            not state.material_evidence_active
            or state.material_claim_basis != payload.material.claim_basis
            or state.material_evidence_id != payload.material.evidence_record_id
            or state.material_evidence_content_sha256 != payload.material.evidence_content_sha256
        ):
            return "material_evidence_invalid"
        if payload.transform_authorization_id is None or state.authorization_id is None:
            return "style_transform_authorization_required"
        if state.authorization_revoked_at is not None:
            return "style_transform_authorization_revoked"
        if (
            state.authorization_expires_at is None
            or state.authorization_expires_at <= self.now_datetime()
        ):
            return "style_transform_authorization_expired"
        if (
            state.authorization_id != payload.transform_authorization_id
            or state.authorization_scope != "local_style_acceptance_only"
        ):
            return "style_transform_authorization_required"
        if (
            state.source_image_set_sha256 != payload.source_image_set_sha256
            or state.authorization_image_set_sha256 != payload.source_image_set_sha256
        ):
            return "style_image_set_mismatch"
        if any(value is not False for value in permission_values.values()):
            return "style_permission_boundary_violation"
        return None

    def _verify_signature(self, request: SignedStyleDeliveryV3) -> None:
        try:
            verify_request_signature(
                secret=self.secret,
                timestamp=request.timestamp,
                now=self.now_seconds(),
                tolerance_seconds=self.tolerance_seconds,
                source_system="classification",
                tenant_id=request.tenant_id,
                method="POST",
                path=DELIVERY_PATH_V3,
                body=request.body,
                supplied_signature=request.signature,
            )
        except InvalidSignature as error:
            raise StyleReceiverFailureV3("invalid_style_warehouse_signature") from error
