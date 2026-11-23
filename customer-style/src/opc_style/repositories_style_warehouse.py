"""Persistence boundary for independent style warehouse intake."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.contracts.style_warehouse_v3 import (
    StyleWarehouseDecisionReceiptV3,
    StyleWarehouseDeliveryV3,
)
from opc_style.models import StyleWarehouseV3Attempt, StyleWarehouseV3Receipt

_LOCKS_GUARD = Lock()
_LOCKS: dict[tuple[str, str], Lock] = defaultdict(Lock)


class StyleWarehouseV3Conflict(ValueError):
    pass


def _delivery_lock(tenant_id: UUID, idempotency_key: str) -> Lock:
    key = (str(tenant_id), idempotency_key)
    with _LOCKS_GUARD:
        return _LOCKS[key]


class StyleWarehouseV3Repository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def accept_or_replay(
        self,
        delivery: StyleWarehouseDeliveryV3,
        *,
        decision: Literal["accepted", "rejected"],
        rejection_code: str | None,
    ) -> StyleWarehouseDecisionReceiptV3:
        key_lock = _delivery_lock(
            delivery.payload.tenant_id, delivery.idempotency_key
        )
        with key_lock:
            existing = self._by_key(
                delivery.payload.tenant_id, delivery.idempotency_key
            )
            if existing is not None:
                return self._replay(existing, delivery.payload_sha256)
            now = datetime.now(UTC)
            receipt = StyleWarehouseDecisionReceiptV3(
                decision_receipt_id=uuid4(),
                handoff_id=delivery.handoff_id,
                checked_payload_sha256=delivery.payload_sha256,
                decision=decision,
                rejection_code=rejection_code,
                generation_job_id=None,
                decided_at=now,
                disposition="accepted",
            )
            record = StyleWarehouseV3Receipt(
                id=receipt.decision_receipt_id,
                tenant_id=delivery.payload.tenant_id,
                handoff_id=delivery.handoff_id,
                idempotency_key=delivery.idempotency_key,
                checked_payload_sha256=delivery.payload_sha256,
                decision=receipt.decision,
                rejection_code=receipt.rejection_code,
                generation_job_id=None,
                payload=receipt.model_dump(mode="json"),
                decided_at=now,
            )
            self.session.add(record)
            try:
                self.session.commit()
            except IntegrityError:
                self.session.rollback()
                winner = self._by_key(
                    delivery.payload.tenant_id, delivery.idempotency_key
                )
                if winner is None:
                    raise
                return self._replay(winner, delivery.payload_sha256)
            return receipt

    def get_receipt(
        self, tenant_id: UUID, receipt_id: UUID
    ) -> StyleWarehouseDecisionReceiptV3 | None:
        record = self.session.scalar(
            select(StyleWarehouseV3Receipt).where(
                StyleWarehouseV3Receipt.id == receipt_id,
                StyleWarehouseV3Receipt.tenant_id == tenant_id,
            )
        )
        if record is None:
            return None
        return StyleWarehouseDecisionReceiptV3.model_validate(record.payload)

    def record_attempt(
        self,
        *,
        tenant_id: UUID | None,
        handoff_id: UUID | None,
        payload_sha256: str | None,
        result: Literal["accepted", "rejected", "duplicate", "conflict", "error"],
        error_code: str | None,
    ) -> None:
        self.session.add(
            StyleWarehouseV3Attempt(
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                payload_sha256=payload_sha256,
                result=result,
                error_code=error_code,
            )
        )
        self.session.commit()

    def _by_key(
        self, tenant_id: UUID, idempotency_key: str
    ) -> StyleWarehouseV3Receipt | None:
        return self.session.scalar(
            select(StyleWarehouseV3Receipt).where(
                StyleWarehouseV3Receipt.tenant_id == tenant_id,
                StyleWarehouseV3Receipt.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _replay(
        record: StyleWarehouseV3Receipt, payload_sha256: str
    ) -> StyleWarehouseDecisionReceiptV3:
        if record.checked_payload_sha256 != payload_sha256:
            raise StyleWarehouseV3Conflict("style_handoff_conflict")
        return StyleWarehouseDecisionReceiptV3.model_validate(record.payload).model_copy(
            update={"disposition": "duplicate"}
        )
