import json
from pathlib import Path
from threading import RLock
from uuid import UUID

from pydantic import ValidationError

from product_engine.contracts.employee_formal_handoff import EmployeeFormalHandoffReceipt
from product_engine.services.local_atomic_json import atomic_write_json


class EmployeeHandoffReceiptFailure(ValueError):
    pass


_EMPLOYEE_HANDOFF_RECEIPT_LOCK = RLock()


class EmployeeHandoffReceiptStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read_all(self) -> list[EmployeeFormalHandoffReceipt]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("employee_handoff_ledger_conflict")
            receipts = [EmployeeFormalHandoffReceipt.model_validate(item) for item in raw]
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise EmployeeHandoffReceiptFailure("employee_handoff_ledger_conflict") from error

        request_ids: set[UUID] = set()
        batch_ids: set[str] = set()
        for receipt in receipts:
            if receipt.request_id in request_ids or receipt.batch_id in batch_ids:
                raise EmployeeHandoffReceiptFailure("employee_handoff_ledger_conflict")
            request_ids.add(receipt.request_id)
            batch_ids.add(receipt.batch_id)
        return receipts

    def latest(self, batch_id: str) -> EmployeeFormalHandoffReceipt | None:
        with _EMPLOYEE_HANDOFF_RECEIPT_LOCK:
            return next(
                (receipt for receipt in self._read_all() if receipt.batch_id == batch_id),
                None,
            )

    def by_request(self, request_id: UUID) -> EmployeeFormalHandoffReceipt | None:
        with _EMPLOYEE_HANDOFF_RECEIPT_LOCK:
            return next(
                (receipt for receipt in self._read_all() if receipt.request_id == request_id),
                None,
            )

    def resolve_replay(
        self,
        request_id: UUID,
        batch_id: str,
        content_sha256: str,
    ) -> EmployeeFormalHandoffReceipt | None:
        with _EMPLOYEE_HANDOFF_RECEIPT_LOCK:
            receipts = self._read_all()
            request_receipt = next(
                (receipt for receipt in receipts if receipt.request_id == request_id),
                None,
            )
            if request_receipt is not None:
                if (
                    request_receipt.batch_id != batch_id
                    or request_receipt.content_sha256 != content_sha256
                ):
                    raise EmployeeHandoffReceiptFailure("employee_handoff_request_conflict")
                return request_receipt.model_copy(update={"disposition": "duplicate"})
            if any(receipt.batch_id == batch_id for receipt in receipts):
                raise EmployeeHandoffReceiptFailure("employee_handoff_ledger_conflict")
            return None

    def save(
        self,
        receipt: EmployeeFormalHandoffReceipt,
    ) -> EmployeeFormalHandoffReceipt:
        with _EMPLOYEE_HANDOFF_RECEIPT_LOCK:
            receipts = self._read_all()
            request_receipt = next(
                (item for item in receipts if item.request_id == receipt.request_id),
                None,
            )
            if request_receipt is not None:
                if (
                    request_receipt.batch_id != receipt.batch_id
                    or request_receipt.content_sha256 != receipt.content_sha256
                    or request_receipt.products != receipt.products
                    or request_receipt.tenant_id != receipt.tenant_id
                ):
                    raise EmployeeHandoffReceiptFailure("employee_handoff_request_conflict")
                return request_receipt.model_copy(update={"disposition": "duplicate"})
            if any(item.batch_id == receipt.batch_id for item in receipts):
                raise EmployeeHandoffReceiptFailure("employee_handoff_ledger_conflict")
            stored = receipt.model_copy(update={"disposition": "created"})
            atomic_write_json(
                self.path,
                [item.model_dump(mode="json") for item in (*receipts, stored)],
            )
            return stored
