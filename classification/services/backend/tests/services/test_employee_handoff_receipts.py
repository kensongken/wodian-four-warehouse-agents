import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from product_engine.contracts.employee_formal_handoff import (
    EmployeeFormalHandoffReceipt,
    EmployeePromotionAssetReceipt,
    EmployeePromotionProductReceipt,
)
from product_engine.services.employee_handoff_receipts import (
    EmployeeHandoffReceiptFailure,
    EmployeeHandoffReceiptStore,
)

REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
BATCH_ID = "BATCH-20260921035620-3A284AA4"
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")


def receipt(
    *,
    request_id: UUID = REQUEST_ID,
    content_sha256: str = "a" * 64,
    batch_id: str = BATCH_ID,
) -> EmployeeFormalHandoffReceipt:
    return EmployeeFormalHandoffReceipt(
        request_id=request_id,
        tenant_id=TENANT_ID,
        batch_id=batch_id,
        content_sha256=content_sha256,
        products=(
            EmployeePromotionProductReceipt(
                product_code="BCNHDIY914",
                package_version=2,
                event_id=EVENT_ID,
                asset_count=1,
                content_sha256="b" * 64,
                assets=(
                    EmployeePromotionAssetReceipt(
                        item_id=ASSET_ID,
                        asset_id=ASSET_ID,
                        object_key="tenants/t/employee-handoffs/b/p/a",
                        sha256="c" * 64,
                        mime_type="image/jpeg",
                        role="main_candidate",
                        position=0,
                    ),
                ),
            ),
        ),
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
        disposition="created",
    )


def test_receipt_store_round_trips_and_replays_same_request(tmp_path: Path) -> None:
    store = EmployeeHandoffReceiptStore(tmp_path / "employee-formal-handoffs.json")
    created = receipt()
    assert store.save(created) == created
    assert store.latest(created.batch_id) == created
    assert store.by_request(REQUEST_ID) == created

    duplicate = store.save(created)

    assert duplicate.disposition == "duplicate"
    assert duplicate.products == created.products
    assert len(json.loads(store.path.read_text(encoding="utf-8"))) == 1


def test_resolve_replay_returns_duplicate_only_for_exact_identity(tmp_path: Path) -> None:
    store = EmployeeHandoffReceiptStore(tmp_path / "employee-formal-handoffs.json")
    store.save(receipt())
    replay = store.resolve_replay(REQUEST_ID, BATCH_ID, "a" * 64)
    assert replay is not None
    assert replay.disposition == "duplicate"
    assert store.resolve_replay(UUID(int=5), "BATCH-NEW", "a" * 64) is None


def test_same_request_with_changed_content_fails_closed(tmp_path: Path) -> None:
    store = EmployeeHandoffReceiptStore(tmp_path / "employee-formal-handoffs.json")
    store.save(receipt())
    with pytest.raises(
        EmployeeHandoffReceiptFailure,
        match="employee_handoff_request_conflict",
    ):
        store.save(receipt(content_sha256="d" * 64))


def test_same_batch_with_another_request_fails_closed(tmp_path: Path) -> None:
    store = EmployeeHandoffReceiptStore(tmp_path / "employee-formal-handoffs.json")
    store.save(receipt())
    with pytest.raises(
        EmployeeHandoffReceiptFailure,
        match="employee_handoff_ledger_conflict",
    ):
        store.save(receipt(request_id=UUID(int=5)))


def test_duplicate_batch_records_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "employee-formal-handoffs.json"
    first = receipt().model_dump(mode="json")
    second = receipt(request_id=UUID(int=5)).model_dump(mode="json")
    path.write_text(json.dumps([first, second]), encoding="utf-8")
    with pytest.raises(
        EmployeeHandoffReceiptFailure,
        match="employee_handoff_ledger_conflict",
    ):
        EmployeeHandoffReceiptStore(path).latest(BATCH_ID)


@pytest.mark.parametrize("content", ["not-json", "{}", '[{"broken":true}]'])
def test_malformed_json_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "employee-formal-handoffs.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(
        EmployeeHandoffReceiptFailure,
        match="employee_handoff_ledger_conflict",
    ):
        EmployeeHandoffReceiptStore(path).latest(BATCH_ID)


def test_atomic_save_leaves_no_writing_file(tmp_path: Path) -> None:
    store = EmployeeHandoffReceiptStore(tmp_path / "employee-formal-handoffs.json")
    store.save(receipt())
    assert not store.path.with_name(f".{store.path.name}.writing").exists()
