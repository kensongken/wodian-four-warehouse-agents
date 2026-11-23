import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_style_warehouse_v3_contract import delivery, eligibility

from opc_style.main import create_app
from opc_style.models import Base, StyleWarehouseV3Attempt, StyleWarehouseV3Receipt

PATH = "/internal/style-warehouse/v3/handoffs"
SECRET = "style-v3-api-test-secret"


def canonical_body(value=None) -> bytes:
    value = value or delivery(
        eligibility(transform_authorization_expires_at=datetime(2099, 1, 1, tzinfo=UTC))
    )
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def signature(
    body: bytes,
    tenant_id: UUID,
    timestamp: int,
    *,
    method: str = "POST",
    path: str = PATH,
) -> str:
    digest = hashlib.sha256(body).hexdigest()
    message = (
        f"{timestamp}\nclassification\n{tenant_id}\n{method}\n{path}\n{digest}"
    ).encode()
    return hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()


def headers(
    body: bytes, *, timestamp: int | None = None, tenant_id=None
) -> dict[str, str]:
    timestamp = timestamp or int(time.time())
    tenant_id = tenant_id or delivery().payload.tenant_id
    return {
        "Content-Type": "application/json",
        "X-Source-System": "classification",
        "X-Tenant-Id": str(tenant_id),
        "X-Signature-Timestamp": str(timestamp),
        "X-Signature": signature(body, tenant_id, timestamp),
    }


def receipt_headers(path: str, *, tenant_id=None) -> dict[str, str]:
    timestamp = int(time.time())
    tenant_id = tenant_id or delivery().payload.tenant_id
    return {
        "X-Source-System": "classification",
        "X-Tenant-Id": str(tenant_id),
        "X-Signature-Timestamp": str(timestamp),
        "X-Signature": signature(b"", tenant_id, timestamp, method="GET", path=path),
    }


def client(monkeypatch, database_path: Path) -> TestClient:
    url = f"sqlite:///{database_path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("STYLE_WAREHOUSE_V3_ENABLED", "true")
    monkeypatch.setenv("STYLE_WAREHOUSE_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("STYLE_WAREHOUSE_VERIFY_SOURCE_IMAGES", "false")
    monkeypatch.setenv("REAL_IMAGE_ENABLED", "false")
    return TestClient(create_app())


def test_signed_delivery_accepts_then_replays_one_receipt(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "style.db"
    app = client(monkeypatch, database_path)
    body = canonical_body()

    first = app.post(PATH, content=body, headers=headers(body))
    replay = app.post(PATH, content=body, headers=headers(body))

    assert first.status_code == 202
    assert first.json()["disposition"] == "accepted"
    assert first.json()["decision"] == "accepted"
    assert replay.status_code == 200
    assert replay.json()["disposition"] == "duplicate"
    assert replay.json()["decision_receipt_id"] == first.json()["decision_receipt_id"]
    with Session(create_engine(f"sqlite:///{database_path}")) as session:
        attempts = session.scalars(
            select(StyleWarehouseV3Attempt).order_by(
                StyleWarehouseV3Attempt.received_at
            )
        ).all()
    assert [attempt.result for attempt in attempts] == ["accepted", "duplicate"]


def test_receipt_survives_application_restart(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "restart.db"
    body = canonical_body()
    first_client = client(monkeypatch, database_path)
    first = first_client.post(PATH, content=body, headers=headers(body))
    first_client.close()

    restarted = client(monkeypatch, database_path)
    replay = restarted.post(PATH, content=body, headers=headers(body))
    receipt_path = (
        f"/internal/style-warehouse/v3/receipts/{first.json()['decision_receipt_id']}"
    )
    unsigned = restarted.get(receipt_path)
    restored = restarted.get(receipt_path, headers=receipt_headers(receipt_path))
    wrong_tenant = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    hidden = restarted.get(
        receipt_path,
        headers=receipt_headers(receipt_path, tenant_id=wrong_tenant),
    )

    assert replay.json()["disposition"] == "duplicate"
    assert unsigned.status_code == 401
    assert restored.status_code == 200
    assert restored.json()["decision_receipt_id"] == first.json()["decision_receipt_id"]
    assert hidden.status_code == 404


def test_changed_content_with_same_key_is_conflict(monkeypatch, tmp_path) -> None:
    app = client(monkeypatch, tmp_path / "conflict.db")
    original = canonical_body()
    app.post(PATH, content=original, headers=headers(original))
    changed = delivery(
        eligibility(
            material={
                "value": "changed-synthetic-material",
                "claim_basis": "merchant_declared",
                "evidence_record_id": "55555555-5555-4555-8555-555555555555",
                "evidence_content_sha256": "d" * 64,
            }
        )
    )
    changed_body = canonical_body(changed)

    response = app.post(PATH, content=changed_body, headers=headers(changed_body))

    assert response.status_code == 409
    assert response.json()["error_code"] == "style_handoff_conflict"


def test_invalid_and_expired_signatures_fail_without_receipt(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "signature.db"
    app = client(monkeypatch, database_path)
    body = canonical_body()
    invalid_headers = headers(body)
    invalid_headers["X-Signature"] = "0" * 64

    invalid = app.post(PATH, content=body, headers=invalid_headers)
    expired = app.post(
        PATH,
        content=body,
        headers=headers(body, timestamp=int(time.time()) - 301),
    )

    assert invalid.status_code == expired.status_code == 401
    assert invalid.json()["error_code"] == "invalid_style_warehouse_signature"
    assert "traceback" not in str(invalid.json()).casefold()
    with Session(create_engine(f"sqlite:///{database_path}")) as session:
        assert (
            session.scalar(select(func.count()).select_from(StyleWarehouseV3Receipt))
            == 0
        )
        attempts = session.scalars(select(StyleWarehouseV3Attempt)).all()
    assert len(attempts) == 2
    assert {attempt.result for attempt in attempts} == {"error"}
    assert {attempt.error_code for attempt in attempts} == {
        "invalid_style_warehouse_signature"
    }


def test_wrong_tenant_and_permission_escalation_fail_closed(
    monkeypatch, tmp_path
) -> None:
    app = client(monkeypatch, tmp_path / "boundary.db")
    body = canonical_body()
    wrong_tenant = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    tenant_response = app.post(
        PATH, content=body, headers=headers(body, tenant_id=wrong_tenant)
    )
    raw = json.loads(body)
    raw["payload"]["automatic_generation_allowed"] = True
    escalated = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    permission_response = app.post(PATH, content=escalated, headers=headers(escalated))

    assert tenant_response.status_code == 409
    assert tenant_response.json()["error_code"] == "style_tenant_mismatch"
    assert permission_response.status_code == 422
    assert permission_response.json()["error_code"] == "style_payload_invalid"
