import json
from uuid import uuid4

from .test_local_price_confirmation import confirmation_payload, seeded_client


def green_package(client) -> bytes:
    confirmed = client.post(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
        json=confirmation_payload(),
    )
    assert confirmed.status_code == 201
    downloaded = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-review-package")
    assert downloaded.status_code == 200
    return downloaded.content


def test_acceptance_form_module_is_served_locally(tmp_path):
    response = seeded_client(tmp_path).get("/local/assets/pilot-acceptance-form.mjs")
    assert response.status_code == 200
    assert "createPilotAcceptanceFormState" in response.text


def submit(
    client,
    package: bytes,
    *,
    request_id: str | None = None,
    note: str = "仅进入脱敏本地小试",
):
    return client.post(
        "/local/api/pilot-review-package/acceptances",
        files={"file": ("review.json", package, "application/json")},
        data={
            "request_id": request_id or str(uuid4()),
            "decision": "accepted",
            "reviewer_id": "reviewer-01",
            "note": note,
        },
    )


def test_green_package_creates_accepted_receipt(tmp_path):
    client = seeded_client(tmp_path)
    response = submit(client, green_package(client))
    assert response.status_code == 201
    payload = response.json()
    assert payload["receipt"]["decision"] == "accepted"
    assert payload["receipt"]["publication_allowed"] is False
    assert payload["message_zh"] == "已记录；仅可进入脱敏本地小试，不代表发布许可。"


def test_exact_replay_returns_same_receipt_and_changed_content_conflicts(tmp_path):
    client = seeded_client(tmp_path)
    package = green_package(client)
    request_id = str(uuid4())
    first = submit(client, package, request_id=request_id)
    replay = submit(client, package, request_id=request_id)
    conflict = submit(client, package, request_id=request_id, note="另一意见")
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["receipt"]["receipt_id"] == first.json()["receipt"]["receipt_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "acceptance_request_conflict"


def test_tampered_package_cannot_write_receipt(tmp_path):
    client = seeded_client(tmp_path)
    package = json.loads(green_package(client))
    package["content_package"]["draft"]["approved_attributes"]["selling_points"].append(
        "被改动的内容"
    )
    response = submit(client, json.dumps(package, ensure_ascii=False).encode())
    assert response.status_code == 409
    assert response.json()["error_code"] == "acceptance_not_allowed"
    assert not (tmp_path / "local-data" / "pilot-acceptances.jsonl").exists()


def test_history_returns_current_receipt_and_version(tmp_path):
    client = seeded_client(tmp_path)
    created = submit(client, green_package(client))
    response = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-acceptances")
    assert response.status_code == 200
    payload = response.json()["history"]
    assert payload["current_decision"] == "accepted"
    assert payload["receipts"][0]["stale"] is False
    assert (
        payload["receipts"][0]["receipt"]["receipt_id"] == created.json()["receipt"]["receipt_id"]
    )


def test_invalid_decision_returns_stable_local_error(tmp_path):
    client = seeded_client(tmp_path)
    response = client.post(
        "/local/api/pilot-review-package/acceptances",
        files={"file": ("review.json", green_package(client), "application/json")},
        data={
            "request_id": str(uuid4()),
            "decision": "publish",
            "reviewer_id": "reviewer-01",
            "note": "不受支持的结论",
        },
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_pilot_acceptance"


def test_oversized_package_is_rejected_without_storage(tmp_path):
    client = seeded_client(tmp_path)
    response = submit(client, b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert response.json()["error_code"] == "pilot_review_package_too_large"
    assert not (tmp_path / "local-data" / "pilot-acceptances.jsonl").exists()


def test_corrupt_acceptance_ledger_fails_history_closed(tmp_path):
    client = seeded_client(tmp_path)
    ledger = tmp_path / "local-data" / "pilot-acceptances.jsonl"
    ledger.write_bytes(b"\xff\xfe\n")
    response = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET/pilot-acceptances")
    assert response.status_code == 422
    assert response.json()["error_code"] == "pilot_acceptance_ledger_unavailable"
