import json
from uuid import UUID

import pytest

from product_engine.services.local_pilot_acceptances import (
    LocalPilotAcceptanceFailure,
    LocalPilotAcceptanceStore,
)

REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")


def record(store, **changes):
    values = {
        "request_id": REQUEST_ID,
        "template_code": "LOCAL-GREEN",
        "source_version": 2,
        "package_sha256": "a" * 64,
        "inspection_code": "verified_current",
        "inspection_signal": "green",
        "decision": "accepted",
        "reviewer_id": "reviewer-01",
        "note": "本地脱敏小试通过",
    }
    values.update(changes)
    return store.record(**values)


def test_records_frozen_receipt_with_safety_boundaries(tmp_path):
    ledger_path = tmp_path / "pilot-acceptances.jsonl"
    store = LocalPilotAcceptanceStore(ledger_path)

    result = store.record(
        request_id=REQUEST_ID,
        template_code="LOCAL-GREEN",
        source_version=2,
        package_sha256="a" * 64,
        inspection_code="verified_current",
        inspection_signal="green",
        decision="accepted",
        reviewer_id=" reviewer-01 ",
        note=" 本地脱敏小试通过 ",
    )

    assert result.created is True
    assert result.receipt.reviewer_id == "reviewer-01"
    assert result.receipt.note == "本地脱敏小试通过"
    assert result.receipt.local_synthetic_only is True
    assert result.receipt.real_customer_data_allowed is False
    assert result.receipt.external_actions_allowed is False
    assert result.receipt.publication_allowed is False
    assert len(ledger_path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("decision", ["accepted", "needs_follow_up", "rejected"])
def test_green_inspection_allows_each_decision(tmp_path, decision):
    result = record(LocalPilotAcceptanceStore(tmp_path / "ledger.jsonl"), decision=decision)
    assert result.receipt.decision == decision


@pytest.mark.parametrize("decision", ["needs_follow_up", "rejected"])
def test_yellow_inspection_allows_only_non_accepting_decisions(tmp_path, decision):
    result = record(
        LocalPilotAcceptanceStore(tmp_path / "ledger.jsonl"),
        inspection_signal="yellow",
        inspection_code="verified_stale",
        decision=decision,
    )
    assert result.receipt.decision == decision


def test_yellow_inspection_rejects_accepted_without_appending(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    with pytest.raises(LocalPilotAcceptanceFailure, match="acceptance_not_allowed"):
        record(
            LocalPilotAcceptanceStore(ledger),
            inspection_signal="yellow",
            inspection_code="verified_stale",
        )
    assert not ledger.exists()


def test_red_inspection_rejects_every_decision_without_appending(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    with pytest.raises(LocalPilotAcceptanceFailure, match="acceptance_not_allowed"):
        record(
            LocalPilotAcceptanceStore(ledger),
            inspection_signal="red",
            inspection_code="package_tampered",
            decision="rejected",
        )
    assert not ledger.exists()


def test_same_request_and_normalized_body_returns_existing_receipt(tmp_path):
    store = LocalPilotAcceptanceStore(tmp_path / "ledger.jsonl")
    first = record(store)
    replay = record(store, reviewer_id=" reviewer-01 ", note=" 本地脱敏小试通过 ")
    assert replay.created is False
    assert replay.receipt.receipt_id == first.receipt.receipt_id
    assert len(store.ledger_path.read_text(encoding="utf-8").splitlines()) == 1


def test_same_request_with_different_body_raises_request_conflict(tmp_path):
    store = LocalPilotAcceptanceStore(tmp_path / "ledger.jsonl")
    record(store)
    with pytest.raises(LocalPilotAcceptanceFailure, match="acceptance_request_conflict"):
        record(store, note="不同意见")
    assert len(store.ledger_path.read_text(encoding="utf-8").splitlines()) == 1


def test_history_marks_old_version_stale_without_rewriting_ledger(tmp_path):
    store = LocalPilotAcceptanceStore(tmp_path / "ledger.jsonl")
    old = record(store, source_version=1)
    before = store.ledger_path.read_bytes()
    history = store.history("LOCAL-GREEN", current_version=2)
    assert history.current_version == 2
    assert history.current_decision is None
    assert history.receipts[0].receipt.receipt_id == old.receipt.receipt_id
    assert history.receipts[0].stale is True
    assert store.ledger_path.read_bytes() == before


@pytest.mark.parametrize(
    ("payload", "binary"),
    [
        (b"\xff\xfe\n", True),
        (b"not-json\n", True),
        (json.dumps({"extra": True}).encode() + b"\n", True),
    ],
)
def test_corrupt_ledger_fails_closed(tmp_path, payload, binary):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(payload)
    store = LocalPilotAcceptanceStore(ledger)
    with pytest.raises(LocalPilotAcceptanceFailure, match="pilot_acceptance_ledger_unavailable"):
        store.history("LOCAL-GREEN", current_version=2)


def test_duplicate_receipt_id_fails_closed(tmp_path):
    store = LocalPilotAcceptanceStore(tmp_path / "ledger.jsonl")
    record(store)
    line = store.ledger_path.read_text(encoding="utf-8")
    store.ledger_path.write_text(line + line, encoding="utf-8")
    with pytest.raises(LocalPilotAcceptanceFailure, match="pilot_acceptance_ledger_unavailable"):
        store.history("LOCAL-GREEN", current_version=2)


def test_stored_request_id_with_conflicting_body_fails_closed(tmp_path):
    store = LocalPilotAcceptanceStore(tmp_path / "ledger.jsonl")
    first = record(store).receipt.model_dump(mode="json")
    second = dict(first, receipt_id="22222222-2222-4222-8222-222222222222", note="冲突")
    store.ledger_path.write_text(
        "\n".join([json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False)])
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(LocalPilotAcceptanceFailure, match="pilot_acceptance_ledger_unavailable"):
        store.history("LOCAL-GREEN", current_version=2)
