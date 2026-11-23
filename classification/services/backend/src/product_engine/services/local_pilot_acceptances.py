import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

AcceptanceDecision = Literal["accepted", "needs_follow_up", "rejected"]
InspectionSignal = Literal["green", "yellow", "red"]


class LocalPilotAcceptanceFailure(ValueError):
    pass


class LocalPilotAcceptanceReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1-local-pilot-acceptance"] = "0.1-local-pilot-acceptance"
    receipt_id: UUID
    request_id: UUID
    template_code: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection_code: str = Field(min_length=1)
    decision: AcceptanceDecision
    reviewer_id: str = Field(min_length=1)
    note: str = Field(min_length=1)
    recorded_at: datetime
    local_synthetic_only: Literal[True] = True
    real_customer_data_allowed: Literal[False] = False
    external_actions_allowed: Literal[False] = False
    publication_allowed: Literal[False] = False

    @field_validator("template_code", "inspection_code", "reviewer_id", "note", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class LocalPilotAcceptanceRecordResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: LocalPilotAcceptanceReceipt
    created: bool


class LocalPilotAcceptanceView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: LocalPilotAcceptanceReceipt
    stale: bool


class LocalPilotAcceptanceHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template_code: str
    current_version: int | None
    current_decision: AcceptanceDecision | None
    receipts: list[LocalPilotAcceptanceView]


class LocalPilotAcceptanceStore:
    def __init__(self, ledger_path: Path) -> None:
        self.ledger_path = ledger_path
        self._lock = RLock()

    @staticmethod
    def _submission_identity(receipt: LocalPilotAcceptanceReceipt) -> tuple[object, ...]:
        return (
            receipt.request_id,
            receipt.template_code,
            receipt.source_version,
            receipt.package_sha256,
            receipt.inspection_code,
            receipt.decision,
            receipt.reviewer_id,
            receipt.note,
        )

    def _read_all(self) -> list[LocalPilotAcceptanceReceipt]:
        if not self.ledger_path.exists():
            return []
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
            receipts = [
                LocalPilotAcceptanceReceipt.model_validate(json.loads(line))
                for line in lines
                if line.strip()
            ]
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
            raise LocalPilotAcceptanceFailure("pilot_acceptance_ledger_unavailable") from error
        receipt_ids: set[UUID] = set()
        request_identities: dict[UUID, tuple[object, ...]] = {}
        for receipt in receipts:
            if receipt.receipt_id in receipt_ids:
                raise LocalPilotAcceptanceFailure("pilot_acceptance_ledger_unavailable")
            receipt_ids.add(receipt.receipt_id)
            identity = self._submission_identity(receipt)
            previous = request_identities.get(receipt.request_id)
            if previous is not None and previous != identity:
                raise LocalPilotAcceptanceFailure("pilot_acceptance_ledger_unavailable")
            request_identities[receipt.request_id] = identity
        return receipts

    def record(
        self,
        *,
        request_id: UUID,
        template_code: str,
        source_version: int,
        package_sha256: str,
        inspection_code: str,
        inspection_signal: InspectionSignal,
        decision: AcceptanceDecision,
        reviewer_id: str,
        note: str,
    ) -> LocalPilotAcceptanceRecordResult:
        if inspection_signal == "red" or (inspection_signal == "yellow" and decision == "accepted"):
            raise LocalPilotAcceptanceFailure("acceptance_not_allowed")
        try:
            receipt = LocalPilotAcceptanceReceipt(
                receipt_id=uuid4(),
                request_id=request_id,
                template_code=template_code,
                source_version=source_version,
                package_sha256=package_sha256,
                inspection_code=inspection_code,
                decision=decision,
                reviewer_id=reviewer_id,
                note=note,
                recorded_at=datetime.now(UTC),
            )
        except ValidationError as error:
            raise LocalPilotAcceptanceFailure("invalid_pilot_acceptance") from error
        with self._lock:
            receipts = self._read_all()
            identity = self._submission_identity(receipt)
            for existing in receipts:
                if existing.request_id != request_id:
                    continue
                if self._submission_identity(existing) == identity:
                    return LocalPilotAcceptanceRecordResult(receipt=existing, created=False)
                raise LocalPilotAcceptanceFailure("acceptance_request_conflict")
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as stream:
                stream.write(receipt.model_dump_json() + "\n")
        return LocalPilotAcceptanceRecordResult(receipt=receipt, created=True)

    def history(
        self,
        template_code: str,
        current_version: int | None,
    ) -> LocalPilotAcceptanceHistory:
        with self._lock:
            receipts = [
                receipt for receipt in self._read_all() if receipt.template_code == template_code
            ]
        receipts.sort(key=lambda item: (item.recorded_at, str(item.receipt_id)), reverse=True)
        views = [
            LocalPilotAcceptanceView(
                receipt=receipt,
                stale=current_version is None or receipt.source_version != current_version,
            )
            for receipt in receipts
        ]
        current_decision = next(
            (
                view.receipt.decision
                for view in views
                if current_version is not None and view.receipt.source_version == current_version
            ),
            None,
        )
        return LocalPilotAcceptanceHistory(
            template_code=template_code,
            current_version=current_version,
            current_decision=current_decision,
            receipts=views,
        )
