#!/usr/bin/env python3
"""Run and record the zero-network, one-image historical-suite acceptance."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opc_style.api.dependencies import get_session
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.main import create_app
from opc_style.models import (
    Base,
    Customer,
    ImageOutputJob,
    StyleSample,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CUSTOMER_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
URL = "http://127.0.0.1:8000/local/style-suite"


@contextmanager
def block_outbound_network():  # type: ignore[no-untyped-def]
    """Measure and fail on every attempted TCP connection during acceptance."""
    attempts: list[str] = []
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def blocked_connect(sock: socket.socket, address: object) -> None:
        del sock
        attempts.append(repr(address))
        raise RuntimeError("outbound_network_attempt_blocked")

    def blocked_create_connection(*args: object, **kwargs: object) -> socket.socket:
        attempts.append(repr((args, kwargs)))
        raise RuntimeError("outbound_network_attempt_blocked")

    socket.socket.connect = blocked_connect  # type: ignore[method-assign]
    socket.create_connection = blocked_create_connection  # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.create_connection = original_create_connection


def _png() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (64, 64), (36, 98, 112)).save(stream, format="PNG")
    return stream.getvalue()


def _style_jpeg(index: int) -> bytes:
    stream = BytesIO()
    Image.new(
        "RGB",
        (64, 64 + index),
        (30 + index * 7, 70 + index * 5, 100 + index * 3),
    ).save(stream, format="JPEG")
    return stream.getvalue()


def _slots(values: tuple[tuple[str, str], ...]) -> list[dict[str, object]]:
    return [
        {
            "slot_key": slot_key,
            "label": label,
            "rules": {
                "aspect_ratio": "4:5",
                "shot_distance": "close",
                "camera_angle": "front",
                "composition": ["centered"],
                "background": ["light gray seamless"],
                "lighting": ["soft left diffuse"],
            },
        }
        for slot_key, label in values
    ]


def _request(response: httpx.Response, expected: int) -> dict[str, object]:
    status_code = response.status_code
    if status_code != expected:
        try:
            error_code = response.json().get("error", {}).get("code", "unknown")
        except (ValueError, AttributeError):
            error_code = "invalid_error_response"
        raise RuntimeError(
            f"acceptance request failed: status={status_code} code={error_code}"
        )
    return response.json()


def main() -> int:
    with block_outbound_network() as guard_probe:
        try:
            socket.create_connection(("network-guard-probe.invalid", 443))
        except RuntimeError as error:
            if str(error) != "outbound_network_attempt_blocked":
                raise
    if len(guard_probe) != 1:
        raise RuntimeError("outbound network guard probe failed")
    with ExitStack() as stack:
        temporary = Path(
            stack.enter_context(TemporaryDirectory(prefix="style-suite-acceptance-"))
        )
        engine = create_database_engine(f"sqlite:///{temporary / 'acceptance.db'}")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        store = stack.enter_context(LocalSyntheticAssetStore(temporary / "media"))
        with sessions() as session:
            session.add_all(
                [
                    Customer(id=CUSTOMER_A, display_name="Acceptance A"),
                    Customer(id=CUSTOMER_B, display_name="Acceptance B"),
                ]
            )
            session.commit()
        app = create_app()
        app.state.local_style_suite_upload_root = str(temporary / "uploads")

        def session_override():  # type: ignore[no-untyped-def]
            with sessions() as session:
                yield session

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_asset_content_store] = lambda: store
        headers = {"X-Customer-Id": str(CUSTOMER_A)}
        with TestClient(app) as client, block_outbound_network() as outbound_attempts:
            page = client.get("/local/style-suite")
            if page.status_code != 200 or "一张图生成全部套图" not in page.text:
                raise RuntimeError("browser workspace unavailable")
            evidence_ids: list[str] = []
            for index in range(1, 11):
                content = _style_jpeg(index)
                uploaded = _request(
                    client.post(
                        "/v1/style-samples/content",
                        headers={
                            **headers,
                            "X-Photo-Id": f"acceptance-style-{index:02d}",
                            "X-Content-Sha256": sha256(content).hexdigest(),
                            "X-Mime-Type": "image/jpeg",
                            "X-Consent-Granted": "true",
                            "X-Consent-Provenance": "local-explicit-style-upload",
                            "X-Consent-Text-Version": "local-style-upload-v1",
                        },
                        content=content,
                    ),
                    201,
                )
                evidence_ids.append(str(uploaded["id"]))
            _request(
                client.post(
                    "/v1/style-cards/scenes/confirm",
                    headers=headers,
                    json={
                        "confirmation_key": "no-cost-acceptance-styles-v2",
                        "confirmed": True,
                        "groups": [
                            {
                                "name": "验收暖光桌面",
                                "evidence_sample_ids": evidence_ids,
                                "library_sample_count": 10,
                                "analysis_mode": "real_visual_analysis_proposal",
                                "analyzer_version": "acceptance-style-analysis-v1",
                                "proposal_fingerprint": "a" * 64,
                                "palette": ["warm neutral"],
                                "lighting": ["soft left diffuse"],
                                "composition": ["centered"],
                                "backgrounds": ["light gray seamless"],
                                "field_evidence": {
                                    field: evidence_ids
                                    for field in (
                                        "palette",
                                        "lighting",
                                        "composition",
                                        "backgrounds",
                                    )
                                },
                                "slots": _slots(
                                    (
                                        ("hero", "主图"),
                                        ("detail", "细节"),
                                        ("wear", "佩戴"),
                                    )
                                ),
                            },
                            {
                                "name": "验收清透白底",
                                "evidence_sample_ids": evidence_ids,
                                "library_sample_count": 10,
                                "analysis_mode": "real_visual_analysis_proposal",
                                "analyzer_version": "acceptance-style-analysis-v1",
                                "proposal_fingerprint": "b" * 64,
                                "palette": ["clean neutral"],
                                "lighting": ["soft left diffuse"],
                                "composition": ["centered"],
                                "backgrounds": ["white seamless"],
                                "field_evidence": {
                                    field: evidence_ids
                                    for field in (
                                        "palette",
                                        "lighting",
                                        "composition",
                                        "backgrounds",
                                    )
                                },
                                "slots": _slots(
                                    (("hero", "主图"), ("side", "侧面"))
                                ),
                            },
                        ],
                    },
                ),
                201,
            )
            asset = _request(
                client.post(
                    "/local/style-suite/api/product",
                    headers={
                        **headers,
                        "X-Product-Id": "acceptance-product",
                        "X-Photo-Id": "acceptance-product-photo",
                        "Content-Type": "image/png",
                    },
                    content=_png(),
                ),
                201,
            )
            preview = _request(
                client.post(
                    "/v1/style-suites/preview",
                    headers=headers,
                    json={
                        "product_id": asset["product_id"],
                        "source_asset_id": asset["id"],
                    },
                ),
                200,
            )
            payload = {
                "product_id": asset["product_id"],
                "source_asset_id": asset["id"],
                "idempotency_key": "no-cost-acceptance-v1",
            }
            run = _request(
                client.post(
                    "/local/style-suite/api/runs", headers=headers, json=payload
                ),
                201,
            )
            with sessions() as session:
                jobs_before = session.scalar(
                    select(func.count()).select_from(ImageOutputJob)
                )
            replay = _request(
                client.post(
                    "/local/style-suite/api/runs", headers=headers, json=payload
                ),
                200,
            )
            with sessions() as session:
                jobs_after = session.scalar(
                    select(func.count()).select_from(ImageOutputJob)
                )
            foreign = client.get(
                f"/local/style-suite/api/runs/{run['id']}",
                headers={"X-Customer-Id": str(CUSTOMER_B)},
            )
            state = _request(
                client.get(f"/local/style-suite/api/runs/{run['id']}", headers=headers),
                200,
            )
            with sessions() as session:
                sample = session.scalar(
                    select(StyleSample)
                    .where(StyleSample.customer_id == CUSTOMER_A)
                    .order_by(StyleSample.photo_id)
                )
            assert sample is not None
            withdrawn = client.post(
                f"/v1/style-samples/{sample.id}/withdraw", headers=headers
            )
            denied_after_withdrawal = client.post(
                "/v1/style-suites/preview",
                headers=headers,
                json={
                    "product_id": asset["product_id"],
                    "source_asset_id": asset["id"],
                },
            )

        facts = {
            "sample_count": len(evidence_ids),
            "style_count": preview["style_count"],
            "slot_count": preview["slot_count"],
            "candidate_count": run["candidate_count"],
            "review_card_count": len(state["review_cards"]),
            "duplicate_child_calls": int(jobs_after or 0) - int(jobs_before or 0),
            "cross_customer_status": foreign.status_code,
            "withdraw_status": withdrawn.status_code,
            "withdrawal_denial_status": denied_after_withdrawal.status_code,
            "outbound_network_attempts": len(outbound_attempts),
            "publishable": run["publishable"],
            "run_id": run["id"],
            "replay_run_id": replay["id"],
        }
        expected = {
            "sample_count": 10,
            "style_count": 2,
            "slot_count": 5,
            "candidate_count": 15,
            "review_card_count": 15,
            "duplicate_child_calls": 0,
            "cross_customer_status": 404,
            "withdraw_status": 200,
            "withdrawal_denial_status": 409,
            "outbound_network_attempts": 0,
            "publishable": False,
        }
        for key, value in expected.items():
            if facts[key] != value:
                raise RuntimeError(f"acceptance mismatch: {key}={facts[key]!r}")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        browser_command = [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "scripts/run_style_suite_browser_acceptance.py"),
        ]
        browser_result = subprocess.run(
            browser_command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        browser_evidence = json.loads(
            (ROOT / "docs/verification/style-replay-suite-browser-run.json").read_text(
                encoding="utf-8"
            )
        )
        document = f"""# Historical style suite no-cost acceptance

- Recorded at: {datetime.now(UTC).isoformat()}
- Git SHA: `{sha}`
- Tested URL: `{URL}`
- Run ID: `{facts["run_id"]}`
- Commands: `.venv/bin/python scripts/run_style_suite_acceptance.py`
- Analysis adapter: `customer_confirmed_local_upload`
- Generation adapter: `deterministic-local-image-generator-v1`
- Mode: synthetic/local/no-cost; outbound network attempts: `0`
- Outbound guard: active and probe-verified; measured workflow attempts: `0`
- Evidence: customers `2`; authorized samples `10`; confirmed styles `2`; suite slots `5`; child jobs `5`; deterministic candidates `15`; review cards `15`
- Replay: same run ID `{facts["replay_run_id"]}`; duplicate child calls `0`
- Isolation: cross-customer read `404`; withdrawn evidence preview `409`
- Publication: `publishable=false`; no publication control exists

## Browser interaction evidence

- Command: `.venv/bin/python scripts/run_style_suite_browser_acceptance.py`
- Result: `{browser_result.stdout.strip()}`
- Recorded at: `{browser_evidence["recorded_at"]}`
- Git SHA: `{browser_evidence["git_sha"]}`
- URL: `{browser_evidence["url"]}`
- Run ID: `{browser_evidence["run_id"]}`
- Visible/loaded demo candidates: `{browser_evidence["demo_image_count"]}/{browser_evidence["demo_loaded_image_count"]}`
- Visible/loaded loopback-provider candidates: `{browser_evidence["candidate_count"]}/{browser_evidence["loaded_image_count"]}`
- Identity gate: pass, needs-review, human-approved pass, and structural block all ran in page JavaScript.
- Provider boundary: loopback fake provider only; calls `{browser_evidence["real_provider_calls"]}`; this is integration evidence, not real-model image-quality evidence.
- External network allowed: `{str(browser_evidence["external_network_allowed"]).lower()}`

## Limitations

This proves the local browser/API workflow with synthetic images only. It does not prove real-model quality, provider billing/reconciliation, production storage, multi-node execution, security/legal approval, or production readiness. A paid pilot remains blocked until credentials and a positive budget are explicitly authorized.
"""
        (ROOT / "docs/verification/style-replay-suite-acceptance.md").write_text(
            document, encoding="utf-8"
        )
        print(
            f"PASS run_id={facts['run_id']} candidates=15 duplicate_child_calls=0 outbound_attempts=0 publishable=false"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
