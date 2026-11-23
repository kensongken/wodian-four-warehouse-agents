import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image

from tests.integrations.test_style_test_bridge_client import (
    ASSET_ID,
    CANDIDATE_BYTES,
    CANDIDATE_ID,
    HANDOFF_ID,
    JOB_ID,
    REAL_BYTES,
    _approved_image_payload,
    _real_image_payload,
    _review_decision_payload,
    _review_job_payload,
)


@pytest.fixture
def review_proxy(tmp_path):
    """Exercise the real client; replace only its external consumer service."""
    from fastapi.responses import Response

    from product_engine.api.local_app import create_local_app
    from product_engine.integrations.style_test_bridge_client import StyleBridgeClient

    consumer = FastAPI()
    state = {"error": None, "status": 503, "reviews": []}

    @consumer.api_route("/{path:path}", methods=["GET", "POST"])
    async def route(path: str, request: Request):
        assert request.headers["x-local-classification-bridge-nonce"] == "private-test-nonce"
        if state["error"]:
            return JSONResponse(status_code=state["status"], content={"detail": state["error"]})
        if request.method == "POST":
            assert path.endswith(f"/{JOB_ID}/candidates/{CANDIDATE_ID}/review")
            body = await request.json()
            state["reviews"].append(body)
            payload = _review_decision_payload()
            payload["decision"] = body["decision"]
            payload["reason"] = body.get("reason")
            if body["decision"] != "approved":
                payload.update(
                    job_status="customer_rejected",
                    candidate_state="customer_rejected",
                    customer_approved=False,
                    product_associations=[],
                    next_job_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    regeneration_count=1,
                    status="awaiting_review",
                    outcome="regenerated",
                    current_session_status="awaiting_review",
                    current_job_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    current_regeneration_count=1,
                )
            return JSONResponse(payload)
        if path.endswith(f"/{JOB_ID}/candidates/{CANDIDATE_ID}/content"):
            return Response(CANDIDATE_BYTES, media_type="image/png")
        if path.endswith(f"/assets/{ASSET_ID}/content"):
            assert request.query_params["job_id"] == str(JOB_ID)
            return Response(REAL_BYTES, media_type="image/jpeg")
        if path.endswith("/products/test-product-bridge/images"):
            assert request.query_params["handoff_id"] == str(HANDOFF_ID)
            view = request.query_params["view"]
            return JSONResponse(
                {
                    "product_id": "test-product-bridge",
                    "requested_view": view,
                    "resolved_view": "real_photo" if view == "real" else "style_image",
                    "images": [
                        _real_image_payload(content_url=True)
                        if view == "real"
                        else _approved_image_payload(content_url=True)
                    ],
                    "publishable": False,
                }
            )
        assert path.endswith(f"/image-outputs/{JOB_ID}")
        return JSONResponse(state.get("job_payload") or _review_job_payload())

    bridge = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        session_nonce="private-test-nonce",
        transport=httpx.ASGITransport(app=consumer),
    )
    return TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_bridge_client=bridge,
        )
    ), state


def test_review_proxy_projects_three_candidates_to_same_origin_without_private_data(review_proxy):
    client, _ = review_proxy
    response = client.get(f"/local/api/style-jobs/{JOB_ID}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "awaiting_review"
    assert len(payload["candidates"]) == 3
    assert payload["synthetic_test_only"] is True
    assert payload["publishable"] is False
    image_url = payload["candidates"][0]["content_url"]
    assert image_url == f"/local/api/style-jobs/{JOB_ID}/candidates/{CANDIDATE_ID}/image"
    assert client.get(image_url).content == CANDIDATE_BYTES
    for private in ("nonce", "artifact_path", "/v1/", "style-bridge.test"):
        assert private not in response.text


def test_review_proxy_preserves_job_scoped_approval_metadata_for_reload(review_proxy):
    client, state = review_proxy
    payload = _review_job_payload()
    payload.update(
        status="approved",
        job_status="customer_approved",
        approved_candidate_id=str(CANDIDATE_ID),
        approved_associations=[
            {
                "product_id": "test-product-bridge",
                "candidate_id": str(CANDIDATE_ID),
                "asset_id": str(ASSET_ID),
            }
        ],
    )
    state["job_payload"] = payload
    response = client.get(f"/local/api/style-jobs/{JOB_ID}")
    assert response.status_code == 200
    assert response.json()["approved_candidate_id"] == str(CANDIDATE_ID)
    assert response.json()["approved_associations"] == payload["approved_associations"]
    assert state["reviews"] == []


@pytest.mark.parametrize(
    "kind,identifier,want,mime",
    [
        ("candidates", CANDIDATE_ID, CANDIDATE_BYTES, "image/png"),
        ("assets", ASSET_ID, REAL_BYTES, "image/jpeg"),
    ],
)
def test_review_proxy_images_return_owned_bytes_without_caching(
    review_proxy, kind, identifier, want, mime
):
    client, _ = review_proxy
    response = client.get(f"/local/api/style-jobs/{JOB_ID}/{kind}/{identifier}/image")
    assert response.status_code == 200
    assert response.content == want
    assert response.headers["content-type"] == mime
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("decision", ["approved", "not_my_style", "product_detail_inaccurate"])
def test_review_proxy_keeps_decision_key_and_named_associations_on_replay(review_proxy, decision):
    client, state = review_proxy
    body = {"decision": decision, "reason": "测试反馈", "idempotency_key": "same-decision-1"}
    url = f"/local/api/style-jobs/{JOB_ID}/candidates/{CANDIDATE_ID}/review"
    first = client.post(url, json=body)
    second = client.post(url, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert state["reviews"] == [body, body]
    payload = first.json()
    assert payload["decision"] == decision
    assert payload["synthetic_test_only"] is True
    assert payload["publishable"] is False
    if decision == "approved":
        association = payload["product_associations"][0]
        assert association["product_id"] == "test-product-bridge"
        assert client.get(association["approved_image"]["content_url"]).content == CANDIDATE_BYTES
        assert client.get(association["real_photo"]["content_url"]).content == REAL_BYTES
    else:
        assert payload["regeneration_count"] == 1
        assert payload["next_job_id"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.mark.parametrize(
    "view,kind,want",
    [
        ("default", "style_image", CANDIDATE_BYTES),
        ("real", "real_photo", REAL_BYTES),
    ],
)
def test_review_proxy_product_views_keep_handoff_and_readable_images(
    review_proxy, view, kind, want
):
    client, _ = review_proxy
    response = client.get(
        f"/local/api/style-products/test-product-bridge/images?handoff_id={HANDOFF_ID}&view={view}"
    )
    assert response.status_code == 200
    assert response.json()["resolved_view"] == kind
    assert client.get(response.json()["images"][0]["content_url"]).content == want


_REVIEW_PROXY_PATHS = [
    ("GET", f"/local/api/style-jobs/{JOB_ID}"),
    ("GET", f"/local/api/style-jobs/{JOB_ID}/candidates/{CANDIDATE_ID}/image"),
    ("GET", f"/local/api/style-jobs/{JOB_ID}/assets/{ASSET_ID}/image"),
    ("POST", f"/local/api/style-jobs/{JOB_ID}/candidates/{CANDIDATE_ID}/review"),
    ("GET", f"/local/api/style-products/test-product-bridge/images?handoff_id={HANDOFF_ID}"),
]


@pytest.mark.parametrize("method,url", _REVIEW_PROXY_PATHS)
def test_review_proxy_disabled_routes_are_absent_even_with_invalid_requests(tmp_path, method, url):
    from product_engine.api.local_app import create_local_app

    client = TestClient(create_local_app(tmp_path, style_test_bridge_enabled=False))
    assert client.request(method, url, json={}).status_code == 404


@pytest.mark.parametrize("method,url", _REVIEW_PROXY_PATHS)
def test_review_proxy_errors_have_stable_chinese_envelope(review_proxy, method, url):
    client, state = review_proxy
    state["error"] = "local_review_content_unavailable"
    response = client.request(method, url, json={"decision": "approved", "idempotency_key": "a"})
    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error_code": "local_review_content_unavailable",
        "retryable": True,
        "message_zh": "图片暂时无法读取，请稍后重试。",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "approved"},
        {"decision": "unknown", "idempotency_key": "a"},
        {"decision": "approved", "idempotency_key": ""},
        {"decision": "approved", "idempotency_key": "a", "customer_id": "forged"},
    ],
)
def test_review_proxy_invalid_decision_does_not_reach_consumer(review_proxy, body):
    client, state = review_proxy
    response = client.post(
        f"/local/api/style-jobs/{JOB_ID}/candidates/{CANDIDATE_ID}/review", json=body
    )
    assert response.status_code == 422
    assert state["reviews"] == []


@pytest.mark.parametrize(
    "key", ["invalid key", "-leading", "_leading", "a/b", "a?b", "a\nb", "中文", "a" * 256]
)
def test_review_proxy_malformed_key_is_rejected_before_consumer(review_proxy, key):
    client, state = review_proxy
    response = client.post(
        f"/local/api/style-jobs/{JOB_ID}/candidates/{CANDIDATE_ID}/review",
        json={"decision": "approved", "idempotency_key": key},
    )
    assert response.status_code == 422
    assert state["reviews"] == []


@pytest.mark.parametrize("key", ["a", "0", "A0._:-z", "a" * 255])
def test_review_proxy_valid_key_boundaries_forward_unchanged(review_proxy, key):
    client, state = review_proxy
    response = client.post(
        f"/local/api/style-jobs/{JOB_ID}/candidates/{CANDIDATE_ID}/review",
        json={"decision": "approved", "idempotency_key": key},
    )
    assert response.status_code == 200
    assert state["reviews"] == [{"decision": "approved", "reason": None, "idempotency_key": key}]


def test_review_proxy_preserves_client_protocol_error_for_downstream_not_found(review_proxy):
    client, state = review_proxy
    state.update(error="not_found", status=404)
    response = client.get(f"/local/api/style-jobs/{JOB_ID}")
    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error_code": "style_bridge_review_job_unexpected_status",
        "retryable": True,
        "message_zh": "本地测试服务暂时不可用，请稍后重试。",
    }


def test_review_proxy_conflict_preserves_nonretryable_feedback(review_proxy):
    client, state = review_proxy
    state.update(error="review_idempotency_conflict", status=409)
    response = client.post(
        f"/local/api/style-jobs/{JOB_ID}/candidates/{CANDIDATE_ID}/review",
        json={"decision": "approved", "idempotency_key": "already-used"},
    )
    assert response.status_code == 503
    assert response.json()["retryable"] is False
    assert response.json()["error_code"] == "review_idempotency_conflict"
    assert response.json()["message_zh"] == "本次选择与已保存的审核记录冲突，请刷新后查看结果。"


@pytest.mark.parametrize("method,url", _REVIEW_PROXY_PATHS)
def test_review_proxy_missing_service_returns_controlled_error(tmp_path, monkeypatch, method, url):
    from product_engine.api.local_app import create_local_app

    monkeypatch.delenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", raising=False)
    monkeypatch.delenv("LOCAL_CLASSIFICATION_BRIDGE_URL", raising=False)
    client = TestClient(create_local_app(tmp_path, style_test_bridge_enabled=True))
    response = client.request(method, url, json={"decision": "approved", "idempotency_key": "a"})
    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error_code": "style_service_unavailable",
        "retryable": True,
        "message_zh": "本地测试服务暂时不可用，请稍后重试。",
    }


def _png_bytes(color: str = "white") -> bytes:
    image = BytesIO()
    Image.new("RGB", (32, 32), color).save(image, format="PNG")
    return image.getvalue()


def _style_test_catalog() -> dict[str, dict[str, str]]:
    return {
        "test-product-turquoise-bracelet": {
            "customer_test_id": "test-customer-local",
            "product_id": "test-product-turquoise-bracelet",
            "product_category": "turquoise",
            "product_form": "bracelet",
        }
    }


def _confirm_request(main_photo_id: str) -> dict[str, object]:
    return {
        "customer_test_id": "test-customer-local",
        "product_id": "test-product-turquoise-bracelet",
        "main_photo_id": main_photo_id,
        "transform_permission": True,
    }


class _CountingBridge:
    def __init__(self) -> None:
        self.health_count = 0
        self.submit_count = 0

    async def health(self) -> bool:
        self.health_count += 1
        return True

    async def stage_and_submit(self, photos, build_handoff):  # type: ignore[no-untyped-def]
        self.submit_count += 1
        receipts = {
            photo.photo_id: type("Receipt", (), {"receipt_id": uuid4()})() for photo in photos
        }
        handoff = build_handoff(receipts)
        return type(
            "Result",
            (),
            {
                "handoff_id": handoff.handoff_id,
                "status": "processing",
                "output_job_ids": (),
                "created": True,
                "publishable": False,
            },
        )()


class _AmbiguousThenSuccessfulBridge(_CountingBridge):
    def __init__(self) -> None:
        super().__init__()
        self.keys: list[str] = []

    async def stage_and_submit(self, photos, build_handoff):  # type: ignore[no-untyped-def]
        from product_engine.integrations.style_test_bridge_client import StyleBridgeError

        self.submit_count += 1
        receipts = {
            photo.photo_id: type("Receipt", (), {"receipt_id": uuid4()})() for photo in photos
        }
        handoff = build_handoff(receipts)
        self.keys.append(handoff.idempotency_key)
        if self.submit_count == 1:
            raise StyleBridgeError("style_bridge_ambiguous_delivery")
        return type(
            "Result",
            (),
            {
                "handoff_id": handoff.handoff_id,
                "status": "processing",
                "output_job_ids": (),
                "created": True,
                "publishable": False,
            },
        )()


@contextmanager
def _running_task7_fake(nonce: str) -> Iterator[tuple[str, dict[str, object]]]:
    """Run the real Task 7 HTTP client against a deterministic loopback consumer."""

    receipt_namespace = UUID("71111111-1111-4111-8111-111111111111")
    output_job_id = UUID("72222222-2222-4222-8222-222222222222")
    state: dict[str, object] = {
        "health_count": 0,
        "media_count": 0,
        "submit_count": 0,
        "poll_count": 0,
        "nonces": [],
        "handoff_id": None,
        "handoffs": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def _read_body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length", "0")))

        def _send_json(self, status_code: int, payload: dict[str, object]) -> None:
            content = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _record_nonce(self) -> bool:
            supplied = self.headers.get("X-Local-Classification-Bridge-Nonce")
            nonces = state["nonces"]
            assert isinstance(nonces, list)
            nonces.append(supplied)
            if supplied == nonce:
                return True
            self._send_json(
                403,
                {"error": {"code": "classification_bridge_nonce_invalid", "retryable": False}},
            )
            return False

        def do_POST(self) -> None:  # noqa: N802
            if not self._record_nonce():
                return
            if self.path.endswith("/media"):
                self._read_body()
                state["media_count"] = int(state["media_count"]) + 1
                photo_id = self.headers["X-Photo-Id"]
                self._send_json(
                    201,
                    {
                        "receipt_id": str(uuid5(receipt_namespace, photo_id)),
                        "photo_id": photo_id,
                        "content_sha256": self.headers["X-Content-Sha256"],
                        "mime_type": self.headers["Content-Type"],
                        "width": 32,
                        "height": 32,
                        "expires_at": "2030-01-01T00:00:00+00:00",
                    },
                )
                return
            if self.path.endswith("/handoffs"):
                payload = json.loads(self._read_body())
                state["submit_count"] = int(state["submit_count"]) + 1
                state["handoff_id"] = payload["handoff_id"]
                handoffs = state["handoffs"]
                assert isinstance(handoffs, list)
                handoffs.append(payload)
                self._send_json(
                    201,
                    {
                        "handoff_id": payload["handoff_id"],
                        "status": "processing",
                        "output_job_ids": [],
                        "created": True,
                        "publishable": False,
                    },
                )
                return
            self._send_json(404, {"ok": False})

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                if not self._record_nonce():
                    return
                state["health_count"] = int(state["health_count"]) + 1
                self._send_json(200, {"status": "ok", "service": "opc-style-agent"})
                return
            if not self._record_nonce():
                return
            handoff_id = state["handoff_id"]
            if handoff_id is None or not self.path.endswith(f"/handoffs/{handoff_id}"):
                self._send_json(404, {"ok": False})
                return
            state["poll_count"] = int(state["poll_count"]) + 1
            current_status = "processing" if state["poll_count"] == 1 else "awaiting_review"
            self._send_json(
                200,
                {
                    "handoff_id": handoff_id,
                    "status": current_status,
                    "output_job_ids": (
                        [] if current_status == "processing" else [str(output_job_id)]
                    ),
                    "output_jobs": []
                    if current_status == "processing"
                    else [
                        {
                            "job_id": str(output_job_id),
                            "output_type": "scene",
                            "status": "awaiting_customer_review",
                        }
                    ],
                    "created": False,
                    "publishable": False,
                },
            )

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_style_bridge_is_absent_by_default_and_progressively_added_when_enabled(tmp_path) -> None:
    from product_engine.api.local_app import create_local_app

    disabled = TestClient(create_local_app(tmp_path / "disabled"))
    assert disabled.get("/local/templates").status_code == 200
    assert 'id="style-bridge-panel"' not in disabled.get("/local/templates").text
    assert disabled.get("/local/assets/photo-usage-preview.mjs").status_code == 404
    assert "style-candidate-review.mjs" not in disabled.get("/local/templates").text
    assert disabled.get("/local/assets/style-candidate-review.mjs").status_code == 404
    assert disabled.get("/local/api/upload-batches/unknown/style-readiness").status_code == 404
    assert (
        disabled.post("/local/api/upload-batches/unknown/confirm-and-generate", json={}).status_code
        == 404
    )
    assert (
        disabled.get("/local/api/style-handoffs/00000000-0000-0000-0000-000000000000").status_code
        == 404
    )

    enabled = TestClient(create_local_app(tmp_path / "enabled", style_test_bridge_enabled=True))
    assert 'id="style-bridge-panel"' in enabled.get("/local/templates").text
    page = enabled.get("/local/templates").text
    assert 'id="employee-package-input"' in page
    assert 'id="review-panel"' in page
    assert "确认商品并生成风格图" in page
    assert "仅限脱敏本地测试" in page
    assert "选择目标批次" in page
    assert "重试查看进度" in page
    bridge_panel = page[page.index('<section class="panel" id="style-bridge-panel"') :]
    assert bridge_panel.count('class="primary"') == 1
    assert "window.prompt" not in page
    assert "readinessStatusMessage" in page
    assert "technicalDetails" in page
    assert enabled.get("/local/assets/photo-usage-preview.mjs").status_code == 200
    assert 'id="style-candidate-reviews"' in page
    review_script = enabled.get("/local/assets/style-candidate-review.mjs")
    assert review_script.status_code == 200
    assert review_script.headers["content-type"].startswith("text/javascript")
    assert enabled.get("/local/api/upload-batches/unknown/style-readiness").status_code == 404


def test_corrupted_jpeg_becomes_a_failed_item_without_a_fake_category_card(tmp_path) -> None:
    from product_engine.api.local_app import create_local_app

    client = TestClient(create_local_app(tmp_path, style_test_bridge_enabled=True))
    created = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()
    batch_id = created["batch"]["batch_id"]
    response = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("broken.jpg", b"\xff\xd8\xffnot-a-jpeg", "image/jpeg")},
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "photo_decode_failed"
    before = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    readiness = client.get(f"/local/api/upload-batches/{batch_id}/style-readiness")
    assert readiness.status_code == 200
    view = readiness.json()
    assert view["items"][0]["state"] == "failed"
    assert view["items"][0]["reason_code"] == "photo_decode_failed"
    after = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert before == after


def test_readiness_health_gate_is_side_effect_free_and_recovers_after_task7_returns(
    tmp_path,
) -> None:
    """Catches enabling confirmation from configuration alone while Task 7 is down."""

    from product_engine.api.local_app import create_local_app

    class RecoveringBridge(_CountingBridge):
        async def health(self) -> bool:
            self.health_count += 1
            return self.health_count > 1

    bridge = RecoveringBridge()
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=bridge,
        )
    )
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("health-gate.png", _png_bytes(), "image/png")},
    )
    before = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]

    unavailable = client.get(f"/local/api/upload-batches/{batch_id}/style-readiness").json()
    after_unavailable = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    recovered = client.get(f"/local/api/upload-batches/{batch_id}/style-readiness").json()
    after_recovery = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]

    assert bridge.health_count == 2
    assert unavailable["can_confirm"] is False
    assert "style_service_unavailable" in unavailable["missing_codes"]
    assert recovered["can_confirm"] is True
    assert "style_service_unavailable" not in recovered["missing_codes"]
    assert before == after_unavailable == after_recovery


def test_confirm_and_generate_marks_unavailable_style_service_retryable_without_handoff(
    tmp_path,
) -> None:
    """A downstream outage must fail closed without recording a successful handoff."""

    from product_engine.api.local_app import create_local_app

    class UnavailableBridge(_CountingBridge):
        async def health(self) -> bool:
            self.health_count += 1
            return False

    bridge = UnavailableBridge()
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=bridge,
        )
    )
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("outage.png", _png_bytes(), "image/png")},
    ).json()["item"]

    response = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json=_confirm_request(uploaded["item_id"]),
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["ok"] is False
    assert payload["ready"] is False
    assert payload["can_confirm"] is False
    assert payload["retryable"] is True
    assert payload["error_code"] == "style_service_unavailable"
    assert "style_service_unavailable" in payload["missing_codes"]
    assert "handoff_id" not in payload
    assert payload["technical_details"]["handoff_id"] is None
    assert bridge.submit_count == 0
    assert not (tmp_path / "style-test-handoffs.json").exists()


def test_failed_and_duplicate_items_can_be_replaced_moved_or_tombstoned(tmp_path) -> None:
    from product_engine.api.local_app import create_local_app

    client = TestClient(create_local_app(tmp_path, style_test_bridge_enabled=True))
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 3}).json()["batch"][
        "batch_id"
    ]
    broken = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("broken.jpg", b"\xff\xd8\xffbad", "image/jpeg")},
    )
    assert broken.status_code == 422
    image = BytesIO()
    Image.new("RGB", (16, 16), "white").save(image, format="PNG")
    replacement = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("replacement.png", image.getvalue(), "image/png")},
    )
    assert replacement.status_code == 201
    first = replacement.json()["item"]
    duplicate = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "1"},
        files={"file": ("duplicate.png", image.getvalue(), "image/png")},
    ).json()["item"]
    readiness = client.get(f"/local/api/upload-batches/{batch_id}/style-readiness").json()
    assert any(view["state"] == "duplicate" for view in readiness["items"])
    assert "photo_conflict_present" in readiness["missing_codes"]
    assert readiness["can_confirm"] is False
    moved = client.post(
        f"/local/api/upload-batches/{batch_id}/items/{first['item_id']}/move",
        json={"to_position": 2},
    )
    assert moved.status_code == 200
    removed = client.delete(f"/local/api/upload-batches/{batch_id}/items/{duplicate['item_id']}")
    assert removed.status_code == 200
    batch = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert {entry["action"] for entry in batch["item_audit"]} >= {"replaced", "moved", "removed"}


def test_cross_batch_move_rejects_unknown_target_without_mutating_source(tmp_path) -> None:
    from product_engine.api.local_app import create_local_app

    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=_CountingBridge(),
        )
    )
    source = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    image = BytesIO()
    Image.new("RGB", (16, 16), "white").save(image, format="PNG")
    item = client.post(
        f"/local/api/upload-batches/{source}/images",
        data={"position": "0"},
        files={"file": ("source.png", image.getvalue(), "image/png")},
    ).json()["item"]
    before = client.get(f"/local/api/upload-batches/{source}").json()["batch"]
    rejected = client.post(
        f"/local/api/upload-batches/{source}/items/{item['item_id']}/move-to-batch",
        json={
            "target_batch_id": "BATCH-NOT-FOUND",
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert rejected.status_code == 404
    assert client.get(f"/local/api/upload-batches/{source}").json()["batch"] == before


def test_cross_batch_move_recalculates_duplicate_state_and_audits_both_batches(
    tmp_path,
) -> None:
    """Catches copying a source-relative duplicate flag into an unrelated target."""

    from product_engine.api.local_app import create_local_app

    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=_CountingBridge(),
        )
    )
    source = client.post("/local/api/upload-batches", json={"expected_count": 3}).json()["batch"][
        "batch_id"
    ]
    target = client.post("/local/api/upload-batches", json={"expected_count": 3}).json()["batch"][
        "batch_id"
    ]
    content = _png_bytes()
    accepted = client.post(
        f"/local/api/upload-batches/{source}/images",
        data={"position": "0"},
        files={"file": ("studio-product.png", content, "image/png")},
    ).json()["item"]
    duplicate = client.post(
        f"/local/api/upload-batches/{source}/images",
        data={"position": "1"},
        files={"file": ("studio-product-copy.png", content, "image/png")},
    ).json()["item"]
    broken = client.post(
        f"/local/api/upload-batches/{source}/images",
        data={"position": "2"},
        files={"file": ("broken.jpg", b"\xff\xd8\xffbad", "image/jpeg")},
    )
    assert broken.status_code == 422
    failed = next(
        item
        for item in client.get(f"/local/api/upload-batches/{source}").json()["batch"]["items"]
        if item["status"] == "failed"
    )

    first_move = client.post(
        f"/local/api/upload-batches/{source}/items/{duplicate['item_id']}/move-to-batch",
        json={
            "target_batch_id": target,
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert first_move.status_code == 200
    moved_first = next(
        view
        for view in first_move.json()["items"]
        if view["item"]["item_id"] == duplicate["item_id"]
    )
    assert moved_first["state"] == "accepted"
    assert moved_first["item"]["status"] == "pending_review"
    assert moved_first["item"]["duplicate_of_item_id"] is None

    second_move = client.post(
        f"/local/api/upload-batches/{source}/items/{accepted['item_id']}/move-to-batch",
        json={
            "target_batch_id": target,
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert second_move.status_code == 200
    moved_second = next(
        view
        for view in second_move.json()["items"]
        if view["item"]["item_id"] == accepted["item_id"]
    )
    assert moved_second["state"] == "duplicate"
    assert moved_second["item"]["status"] == "duplicate"
    assert moved_second["item"]["duplicate_of_item_id"] == duplicate["item_id"]

    third_move = client.post(
        f"/local/api/upload-batches/{source}/items/{failed['item_id']}/move-to-batch",
        json={
            "target_batch_id": target,
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert third_move.status_code == 200
    moved_failed = next(
        view for view in third_move.json()["items"] if view["item"]["item_id"] == failed["item_id"]
    )
    assert moved_failed["state"] == "failed"

    source_after = client.get(f"/local/api/upload-batches/{source}").json()["batch"]
    target_after = client.get(f"/local/api/upload-batches/{target}").json()["batch"]
    assert source_after["items"] == []
    assert source_after["counts"] == {
        "expected": 3,
        "uploaded": 0,
        "success": 0,
        "duplicate": 0,
        "failed": 0,
        "pending_review": 0,
        "reviewed": 0,
    }
    assert target_after["counts"]["success"] == 1
    assert target_after["counts"]["duplicate"] == 1
    assert target_after["counts"]["failed"] == 1
    moved_ids = [duplicate["item_id"], accepted["item_id"], failed["item_id"]]
    assert [entry["item_id"] for entry in source_after["item_audit"]] == moved_ids
    assert [entry["item_id"] for entry in target_after["item_audit"]] == moved_ids
    assert all(entry["action"] == "moved" for entry in source_after["item_audit"])
    assert all(entry["action"] == "moved" for entry in target_after["item_audit"])


def test_removing_a_moved_primary_promotes_its_target_duplicate(tmp_path) -> None:
    """A recovery action must not leave the remaining target photo unusable."""

    from product_engine.api.local_app import create_local_app

    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=_CountingBridge(),
        )
    )
    source = client.post("/local/api/upload-batches", json={"expected_count": 2}).json()["batch"][
        "batch_id"
    ]
    target = client.post("/local/api/upload-batches", json={"expected_count": 2}).json()["batch"][
        "batch_id"
    ]
    content = _png_bytes()
    original = client.post(
        f"/local/api/upload-batches/{source}/images",
        data={"position": "0"},
        files={"file": ("studio-product.png", content, "image/png")},
    ).json()["item"]
    duplicate = client.post(
        f"/local/api/upload-batches/{source}/images",
        data={"position": "1"},
        files={"file": ("studio-product-copy.png", content, "image/png")},
    ).json()["item"]
    move_body = {
        "target_batch_id": target,
        "target_product_id": "test-product-turquoise-bracelet",
    }
    assert (
        client.post(
            f"/local/api/upload-batches/{source}/items/{duplicate['item_id']}/move-to-batch",
            json=move_body,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/local/api/upload-batches/{source}/items/{original['item_id']}/move-to-batch",
            json=move_body,
        ).status_code
        == 200
    )

    removed = client.delete(f"/local/api/upload-batches/{target}/items/{duplicate['item_id']}")

    assert removed.status_code == 200
    remaining = next(
        view for view in removed.json()["items"] if view["item"]["item_id"] == original["item_id"]
    )
    assert remaining["state"] == "accepted"
    assert remaining["item"]["status"] == "pending_review"
    assert remaining["item"]["duplicate_of_item_id"] is None
    assert removed.json()["can_confirm"] is True


def test_cross_batch_move_rebuilds_both_duplicate_chains_without_dangling_ids(
    tmp_path,
) -> None:
    """Catches moving an original while its source duplicates still point to it."""

    from product_engine.api.local_app import create_local_app

    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=_CountingBridge(),
        )
    )
    source = client.post("/local/api/upload-batches", json={"expected_count": 3}).json()["batch"][
        "batch_id"
    ]
    target = client.post("/local/api/upload-batches", json={"expected_count": 3}).json()["batch"][
        "batch_id"
    ]
    content = _png_bytes()

    source_items = [
        client.post(
            f"/local/api/upload-batches/{source}/images",
            data={"position": str(position)},
            files={"file": (f"source-{position}.png", content, "image/png")},
        ).json()["item"]
        for position in range(3)
    ]
    target_items = [
        client.post(
            f"/local/api/upload-batches/{target}/images",
            data={"position": str(position)},
            files={"file": (f"target-{position}.png", content, "image/png")},
        ).json()["item"]
        for position in range(2)
    ]

    moved = client.post(
        f"/local/api/upload-batches/{source}/items/{source_items[0]['item_id']}/move-to-batch",
        json={
            "target_batch_id": target,
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert moved.status_code == 200
    source_after = client.get(f"/local/api/upload-batches/{source}").json()["batch"]
    target_after = client.get(f"/local/api/upload-batches/{target}").json()["batch"]

    source_by_id = {item["item_id"]: item for item in source_after["items"]}
    assert source_by_id[source_items[1]["item_id"]]["status"] == "pending_review"
    assert source_by_id[source_items[1]["item_id"]]["duplicate_of_item_id"] is None
    assert source_by_id[source_items[2]["item_id"]]["status"] == "duplicate"
    assert (
        source_by_id[source_items[2]["item_id"]]["duplicate_of_item_id"]
        == source_items[1]["item_id"]
    )

    target_by_id = {item["item_id"]: item for item in target_after["items"]}
    assert target_by_id[target_items[0]["item_id"]]["status"] == "pending_review"
    assert target_by_id[target_items[0]["item_id"]]["duplicate_of_item_id"] is None
    assert (
        target_by_id[target_items[1]["item_id"]]["duplicate_of_item_id"]
        == target_items[0]["item_id"]
    )
    assert (
        target_by_id[source_items[0]["item_id"]]["duplicate_of_item_id"]
        == target_items[0]["item_id"]
    )

    for batch in (source_after, target_after):
        live_ids = {item["item_id"] for item in batch["items"]}
        assert all(
            item["duplicate_of_item_id"] is None or item["duplicate_of_item_id"] in live_ids
            for item in batch["items"]
        )


def test_cross_batch_position_conflict_has_zero_source_or_target_mutation(tmp_path) -> None:
    """Catches a move that silently chooses another slot after the caller named one."""

    from product_engine.api.local_app import create_local_app

    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=_CountingBridge(),
        )
    )
    source = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    target = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    source_item = client.post(
        f"/local/api/upload-batches/{source}/images",
        data={"position": "0"},
        files={"file": ("studio-product.png", _png_bytes("white"), "image/png")},
    ).json()["item"]
    client.post(
        f"/local/api/upload-batches/{target}/images",
        data={"position": "0"},
        files={"file": ("studio-product-other.png", _png_bytes("black"), "image/png")},
    )
    before_source = client.get(f"/local/api/upload-batches/{source}").json()["batch"]
    before_target = client.get(f"/local/api/upload-batches/{target}").json()["batch"]

    conflict = client.post(
        f"/local/api/upload-batches/{source}/items/{source_item['item_id']}/move-to-batch",
        json={
            "target_batch_id": target,
            "target_product_id": "test-product-turquoise-bracelet",
            "to_position": 0,
        },
    )

    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "position_conflict"
    assert client.get(f"/local/api/upload-batches/{source}").json()["batch"] == before_source
    assert client.get(f"/local/api/upload-batches/{target}").json()["batch"] == before_target


@pytest.mark.parametrize(
    "changed_fact",
    ["photo_hash", "photo_category", "product", "main_photo", "permission", "version"],
)
def test_submitted_handoff_replays_once_and_rejects_durable_fact_drift(
    tmp_path,
    changed_fact: str,
) -> None:
    """Catches a persisted handoff being silently reused after any guarded fact drifts."""

    from product_engine.api.local_app import create_local_app

    bridge = _CountingBridge()
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=bridge,
        )
    )
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("studio-product.png", _png_bytes(), "image/png")},
    ).json()["item"]
    request = _confirm_request(uploaded["item_id"])

    first = client.post(f"/local/api/upload-batches/{batch_id}/confirm-and-generate", json=request)
    replay = client.post(f"/local/api/upload-batches/{batch_id}/confirm-and-generate", json=request)
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["handoff_id"] == first.json()["handoff_id"]
    assert replay.json()["created"] is False
    assert bridge.submit_count == 1

    ledger_path = tmp_path / "upload-batches.json"
    raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    batch = next(entry for entry in raw if entry["batch_id"] == batch_id)
    item = batch["items"][0]
    if changed_fact == "photo_hash":
        item["sha256"] = "f" * 64
    elif changed_fact == "photo_category":
        fact = item["photo_classification_facts"][-1]
        fact["automatic_decision"]["category"] = "scene_photo"
        fact["final_category"] = "scene_photo"
    elif changed_fact == "product":
        batch["style_test_permission"]["product_id"] = "test-product-changed"
        batch["style_test_product_confirmation"]["product_id"] = "test-product-changed"
    elif changed_fact == "main_photo":
        batch["style_test_product_confirmation"]["main_photo_id"] = str(uuid4())
    elif changed_fact == "permission":
        batch["style_test_permission"]["transform_permission"] = False
    else:
        item["photo_classification_facts"][-1]["confirmation_version"] = 2
        batch["style_test_permission"]["confirmation_version"] = 2
        batch["style_test_product_confirmation"]["confirmation_version"] = 2
    ledger_path.write_text(json.dumps(raw), encoding="utf-8")

    conflict = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate", json=request
    )

    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "style_handoff_fingerprint_conflict"
    assert bridge.submit_count == 1


def test_startup_injected_task7_client_confirms_submits_and_polls_durable_statuses(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a page-only fake that bypasses URL, nonce, bytes, or durable polling."""

    from product_engine.api.local_app import create_local_app

    nonce = "task8-local-startup-nonce"
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_TEST_PRODUCT_CATALOG",
        json.dumps(_style_test_catalog()),
    )
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", nonce)
    with _running_task7_fake(nonce) as (base_url, task7):
        monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_URL", base_url)
        client = TestClient(create_local_app(tmp_path))
        batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
            "batch"
        ]["batch_id"]
        uploaded = client.post(
            f"/local/api/upload-batches/{batch_id}/images",
            data={"position": "0"},
            files={"file": ("studio-product.png", _png_bytes(), "image/png")},
        ).json()["item"]

        readiness = client.get(f"/local/api/upload-batches/{batch_id}/style-readiness").json()
        assert readiness["can_confirm"] is True
        assert readiness["ready"] is False
        assert readiness["missing_codes"] == [
            "product_confirmation_required",
            "main_photo_required",
            "transform_permission_required",
        ]

        submitted = client.post(
            f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
            json=_confirm_request(uploaded["item_id"]),
        )
        assert submitted.status_code == 200
        assert submitted.json()["current_status"] == "processing"
        handoff_id = submitted.json()["handoff_id"]

        processing = client.get(f"/local/api/style-handoffs/{handoff_id}")
        awaiting_review = client.get(f"/local/api/style-handoffs/{handoff_id}")
        assert processing.json()["handoff"]["current_status"] == "processing"
        assert awaiting_review.json()["handoff"]["current_status"] == "awaiting_review"
        assert awaiting_review.json()["handoff"]["output_job_ids"] == [
            "72222222-2222-4222-8222-222222222222"
        ]
        assert awaiting_review.json()["handoff"]["output_jobs"] == [
            {
                "job_id": "72222222-2222-4222-8222-222222222222",
                "output_type": "scene",
                "status": "awaiting_customer_review",
            }
        ]

        replay = client.post(
            f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
            json=_confirm_request(uploaded["item_id"]),
        )
        assert replay.status_code == 200
        assert replay.json()["handoff_id"] == handoff_id
        assert replay.json()["created"] is False

        durable = json.loads((tmp_path / "style-test-handoffs.json").read_text())
        assert durable == [
            {
                "batch_id": batch_id,
                "created": True,
                "current_status": "awaiting_review",
                "fingerprint": submitted.json()["fingerprint"],
                "handoff_id": handoff_id,
                "output_job_ids": ["72222222-2222-4222-8222-222222222222"],
                "output_jobs": [
                    {
                        "job_id": "72222222-2222-4222-8222-222222222222",
                        "output_type": "scene",
                        "status": "awaiting_customer_review",
                    }
                ],
                "publishable": False,
            }
        ]
        assert task7["media_count"] == 1
        assert task7["submit_count"] == 1
        assert task7["poll_count"] == 2
        assert task7["health_count"] == 2
        assert task7["nonces"] == [nonce, nonce, nonce, nonce, nonce, nonce]


def test_latest_style_handoff_restores_the_most_recent_submitted_batch(tmp_path) -> None:
    from product_engine.api.local_app import create_local_app

    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=_CountingBridge(),
        )
    )
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("studio-product.png", _png_bytes(), "image/png")},
    ).json()["item"]
    submitted = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json=_confirm_request(uploaded["item_id"]),
    )
    assert submitted.status_code == 200

    latest = client.get("/local/api/latest-style-handoff")

    assert latest.status_code == 200
    assert latest.json()["handoff"]["batch_id"] == batch_id
    page = client.get("/local/templates")
    assert "restoreLatestStyleHandoff" in page.text
    assert "'/local/api/latest-style-handoff'" in page.text


def test_polling_error_preserves_the_handoff_and_retry_reaches_awaiting_review(
    tmp_path,
) -> None:
    """Catches losing a durable handoff when one consumer poll is retryable."""

    from product_engine.api.local_app import create_local_app
    from product_engine.integrations.style_test_bridge_client import StyleBridgeClient

    consumer = FastAPI()
    poll_attempts = 0

    @consumer.get("/health")
    async def health(request: Request) -> dict[str, str]:
        assert request.headers["x-local-classification-bridge-nonce"] == "test-bridge-nonce"
        return {"status": "ok", "service": "opc-style-agent"}

    @consumer.post("/v1/local-test/classification-bridge/media")
    async def stage_media(request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=201,
            content={
                "receipt_id": str(uuid4()),
                "photo_id": request.headers["x-photo-id"],
                "content_sha256": request.headers["x-content-sha256"],
                "mime_type": request.headers["content-type"],
                "width": 32,
                "height": 32,
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        )

    @consumer.post("/v1/local-test/classification-bridge/handoffs")
    async def submit_handoff(request: Request) -> JSONResponse:
        payload = await request.json()
        return JSONResponse(
            status_code=201,
            content={
                "handoff_id": payload["handoff_id"],
                "status": "processing",
                "output_job_ids": [],
                "created": True,
                "publishable": False,
            },
        )

    @consumer.get("/v1/local-test/classification-bridge/handoffs/{handoff_id}")
    async def poll_handoff(handoff_id: UUID) -> JSONResponse:
        nonlocal poll_attempts
        poll_attempts += 1
        if poll_attempts == 1:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "classification_handoff_cleanup_pending",
                        "retryable": True,
                    }
                },
            )
        return JSONResponse(
            content={
                "handoff_id": str(handoff_id),
                "status": "awaiting_review",
                "output_job_ids": ["72222222-2222-4222-8222-222222222222"],
                "created": False,
                "publishable": False,
            }
        )

    bridge = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=consumer),
        session_nonce="test-bridge-nonce",
    )
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=bridge,
        )
    )
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("studio-product.png", _png_bytes(), "image/png")},
    ).json()["item"]
    submitted = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json=_confirm_request(uploaded["item_id"]),
    )
    assert submitted.status_code == 200
    assert submitted.json()["current_status"] == "processing"

    polled = client.get(f"/local/api/style-handoffs/{submitted.json()['handoff_id']}")

    assert polled.status_code == 503
    assert polled.json() == {
        "ok": False,
        "error_code": "classification_handoff_cleanup_pending",
        "retryable": True,
        "message_zh": "暂时无法读取风格图任务，请稍后重试。",
    }
    durable_after_error = json.loads(
        (tmp_path / "style-test-handoffs.json").read_text(encoding="utf-8")
    )
    assert durable_after_error[0]["current_status"] == "processing"

    retried = client.get(f"/local/api/style-handoffs/{submitted.json()['handoff_id']}")

    assert retried.status_code == 200
    assert retried.json()["handoff"]["current_status"] == "awaiting_review"
    assert retried.json()["handoff"]["output_job_ids"] == ["72222222-2222-4222-8222-222222222222"]
    durable_after_retry = json.loads(
        (tmp_path / "style-test-handoffs.json").read_text(encoding="utf-8")
    )
    assert durable_after_retry[0]["current_status"] == "awaiting_review"


def test_open_target_move_binds_product_survives_restart_and_then_submits(tmp_path) -> None:
    """A red duplicate can enter an open target without becoming a dead end."""

    from product_engine.api.local_app import create_local_app

    catalog = _style_test_catalog()
    catalog["test-product-turquoise-bracelet"]["display_label"] = "绿松石手链测试商品"
    catalog["test-product-nanhong-ring"] = {
        "customer_test_id": "test-customer-local",
        "product_id": "test-product-nanhong-ring",
        "product_category": "nanhong",
        "product_form": "ring",
        "display_label": "南红戒指测试商品",
    }
    bridge = _CountingBridge()
    first_session = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=catalog,
            style_bridge_client=bridge,
        )
    )
    source_id = first_session.post("/local/api/upload-batches", json={"expected_count": 2}).json()[
        "batch"
    ]["batch_id"]
    target_id = first_session.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
        "batch"
    ]["batch_id"]
    content = _png_bytes()
    original = first_session.post(
        f"/local/api/upload-batches/{source_id}/images",
        data={"position": "0"},
        files={"file": ("studio-product.png", content, "image/png")},
    ).json()["item"]
    duplicate = first_session.post(
        f"/local/api/upload-batches/{source_id}/images",
        data={"position": "1"},
        files={"file": ("studio-product-copy.png", content, "image/png")},
    ).json()["item"]

    open_targets = first_session.get("/local/api/style-test-target-batches").json()
    target_choice = next(
        target for target in open_targets["targets"] if target["batch_id"] == target_id
    )
    assert target_choice == {
        "batch_id": target_id,
        "product_id": "test-product-turquoise-bracelet",
        "display_label": "绿松石手链测试商品",
    }

    before_invalid_source = first_session.get(f"/local/api/upload-batches/{source_id}").json()[
        "batch"
    ]
    before_invalid_target = first_session.get(f"/local/api/upload-batches/{target_id}").json()[
        "batch"
    ]
    invalid_product = first_session.post(
        f"/local/api/upload-batches/{source_id}/items/{duplicate['item_id']}/move-to-batch",
        json={
            "target_batch_id": target_id,
            "target_product_id": "test-product-not-allowlisted",
        },
    )
    assert invalid_product.status_code == 409
    assert invalid_product.json()["error_code"] == "test_product_not_allowlisted"
    assert (
        first_session.get(f"/local/api/upload-batches/{source_id}").json()["batch"]
        == before_invalid_source
    )
    assert (
        first_session.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
        == before_invalid_target
    )

    moved = first_session.post(
        f"/local/api/upload-batches/{source_id}/items/{duplicate['item_id']}/move-to-batch",
        json={
            "target_batch_id": target_id,
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert moved.status_code == 200
    moved_payload = moved.json()
    moved_view = next(
        view for view in moved_payload["items"] if view["item"]["item_id"] == duplicate["item_id"]
    )
    assert moved_view["state"] == "accepted"
    assert moved_view["recovery_actions"] == ["delete", "move"]
    assert moved_payload["can_confirm"] is True
    assert moved_payload["product_binding"]["product_id"] == ("test-product-turquoise-bracelet")

    before_binding_conflict_source = first_session.get(
        f"/local/api/upload-batches/{source_id}"
    ).json()["batch"]
    before_binding_conflict_target = first_session.get(
        f"/local/api/upload-batches/{target_id}"
    ).json()["batch"]
    binding_conflict = first_session.post(
        f"/local/api/upload-batches/{source_id}/items/{original['item_id']}/move-to-batch",
        json={
            "target_batch_id": target_id,
            "target_product_id": "test-product-nanhong-ring",
        },
    )
    assert binding_conflict.status_code == 409
    assert binding_conflict.json()["error_code"] == "target_product_binding_conflict"
    assert (
        first_session.get(f"/local/api/upload-batches/{source_id}").json()["batch"]
        == before_binding_conflict_source
    )
    assert (
        first_session.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
        == before_binding_conflict_target
    )

    restarted_bridge = _CountingBridge()
    new_session = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=catalog,
            style_bridge_client=restarted_bridge,
        )
    )
    assert new_session.get("/local/templates").status_code == 200
    restarted_targets = new_session.get("/local/api/style-test-target-batches").json()
    assert any(target["batch_id"] == target_id for target in restarted_targets["targets"])

    confirmed = new_session.post(
        f"/local/api/upload-batches/{target_id}/confirm-and-generate",
        json=_confirm_request(duplicate["item_id"]),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["current_status"] == "processing"
    assert restarted_bridge.submit_count == 1

    sealed_readiness = new_session.get(
        f"/local/api/upload-batches/{target_id}/style-readiness"
    ).json()
    sealed_view = next(
        view
        for view in sealed_readiness["items"]
        if view["item"]["item_id"] == duplicate["item_id"]
    )
    assert sealed_readiness["sealed"] is True
    assert sealed_view["recovery_actions"] == []
    sealed_targets = new_session.get("/local/api/style-test-target-batches").json()
    assert all(target["batch_id"] != target_id for target in sealed_targets["targets"])

    before_sealed_source = new_session.get(f"/local/api/upload-batches/{source_id}").json()["batch"]
    before_sealed_target = new_session.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
    sealed_move = new_session.post(
        f"/local/api/upload-batches/{source_id}/items/{original['item_id']}/move-to-batch",
        json={
            "target_batch_id": target_id,
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert sealed_move.status_code == 409
    assert sealed_move.json()["error_code"] == "style_test_batch_sealed"
    assert (
        new_session.get(f"/local/api/upload-batches/{source_id}").json()["batch"]
        == before_sealed_source
    )
    assert (
        new_session.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
        == before_sealed_target
    )


def test_allowlisted_test_product_is_required_before_a_bridge_submission(tmp_path) -> None:
    from product_engine.api.local_app import create_local_app

    class FakeBridge:
        calls = 0

        async def health(self) -> bool:
            return True

        async def stage_and_submit(self, photos, build_handoff):
            self.calls += 1
            receipts = {
                photo.photo_id: type("Receipt", (), {"receipt_id": uuid4()})() for photo in photos
            }
            handoff = build_handoff(receipts)
            return type(
                "Result",
                (),
                {
                    "handoff_id": handoff.handoff_id,
                    "status": "processing",
                    "output_job_ids": (),
                    "created": True,
                    "publishable": False,
                },
            )()

    image = BytesIO()
    Image.new("RGB", (32, 32), "white").save(image, format="PNG")
    bridge = FakeBridge()
    catalog = {
        "test-product-turquoise-bracelet": {
            "customer_test_id": "test-customer-local",
            "product_id": "test-product-turquoise-bracelet",
            "product_category": "turquoise",
            "product_form": "bracelet",
        }
    }
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=catalog,
            style_bridge_client=bridge,
        )
    )
    created = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()
    batch_id = created["batch"]["batch_id"]
    uploaded = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("catalog.png", image.getvalue(), "image/png")},
    ).json()["item"]
    invalid = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json={
            "customer_test_id": "test-customer-local",
            "product_id": "test-product-made-up",
            "main_photo_id": uploaded["item_id"],
            "transform_permission": True,
        },
    )
    assert invalid.status_code == 409
    assert bridge.calls == 0
    batch = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert batch["style_test_permission"] is None
    assert batch["style_test_product_confirmation"] is None

    coerced = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json={
            "customer_test_id": "test-customer-local",
            "product_id": "test-product-turquoise-bracelet",
            "confirmation_version": "1",
            "main_photo_id": uploaded["item_id"],
            "transform_permission": "true",
        },
    )
    assert coerced.status_code == 422
    batch = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert batch["style_test_permission"] is None

    accepted = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json={
            "customer_test_id": "test-customer-local",
            "product_id": "test-product-turquoise-bracelet",
            "main_photo_id": uploaded["item_id"],
            "transform_permission": True,
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["current_status"] == "processing"
    assert bridge.calls == 1
    replay = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json={
            "customer_test_id": "test-customer-local",
            "product_id": "test-product-turquoise-bracelet",
            "main_photo_id": uploaded["item_id"],
            "transform_permission": True,
        },
    )
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert bridge.calls == 1
    handoff = client.get(f"/local/api/style-handoffs/{accepted.json()['handoff_id']}")
    assert handoff.json()["handoff"]["current_status"] == "processing"


def test_same_product_second_batch_gets_v2_and_replays_through_durable_task7(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second photo set for one product must not collide with the first Task 4 key."""

    from product_engine.api.local_app import create_local_app

    nonce = "task8-version-allocation-nonce"
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_TEST_PRODUCT_CATALOG", json.dumps(_style_test_catalog())
    )
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", nonce)
    with _running_task7_fake(nonce) as (base_url, task7):
        monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_URL", base_url)
        client = TestClient(create_local_app(tmp_path))
        submitted: list[dict[str, object]] = []
        for color in ("white", "black"):
            batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
                "batch"
            ]["batch_id"]
            item = client.post(
                f"/local/api/upload-batches/{batch_id}/images",
                data={"position": "0"},
                files={
                    "file": (
                        f"studio-product-{color}.png",
                        _png_bytes(color),
                        "image/png",
                    )
                },
            ).json()["item"]
            response = client.post(
                f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
                json=_confirm_request(item["item_id"]),
            )
            assert response.status_code == 200
            submitted.append({"batch_id": batch_id, "item_id": item["item_id"], **response.json()})

        handoffs = task7["handoffs"]
        assert isinstance(handoffs, list)
        assert [handoff["confirmation_version"] for handoff in handoffs] == [1, 2]
        assert [handoff["idempotency_key"] for handoff in handoffs] == [
            "style-test:test-product-turquoise-bracelet:v1",
            "style-test:test-product-turquoise-bracelet:v2",
        ]
        assert task7["submit_count"] == 2

        second = submitted[1]
        replay = client.post(
            f"/local/api/upload-batches/{second['batch_id']}/confirm-and-generate",
            json=_confirm_request(str(second["item_id"])),
        )
        assert replay.status_code == 200
        assert replay.json()["handoff_id"] == second["handoff_id"]
        assert replay.json()["created"] is False
        assert task7["submit_count"] == 2

        restarted = TestClient(create_local_app(tmp_path))
        readiness = restarted.get(
            f"/local/api/upload-batches/{second['batch_id']}/style-readiness"
        ).json()
        assert readiness["batch_confirmation_version"] == 2
        batch = restarted.get(f"/local/api/upload-batches/{second['batch_id']}").json()["batch"]
        assert batch["style_test_permission"]["confirmation_version"] == 2
        assert batch["style_test_product_confirmation"]["confirmation_version"] == 2


def test_correction_carries_every_active_photo_to_one_batch_version_and_retries(
    tmp_path,
) -> None:
    """Changing one card advances the whole batch, preserving every v1 fact."""

    from product_engine.api.local_app import create_local_app

    bridge = _CountingBridge()
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=bridge,
        )
    )
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 2}).json()["batch"][
        "batch_id"
    ]
    low = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("ambient-product.png", _png_bytes("white"), "image/png")},
    ).json()["item"]
    high = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "1"},
        files={"file": ("studio-product.png", _png_bytes("black"), "image/png")},
    ).json()["item"]

    corrected = client.post(
        f"/local/api/upload-batches/{batch_id}/items/{low['item_id']}/photo-category-corrections",
        json={"corrected_category": "real_photo", "reviewer_note": "审核后确认为商品实拍。"},
    )
    assert corrected.status_code == 200
    assert corrected.json()["batch_confirmation_version"] == 2
    assert corrected.json()["can_confirm"] is True

    batch = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    by_id = {item["item_id"]: item for item in batch["items"]}
    assert [
        fact["confirmation_version"] for fact in by_id[low["item_id"]]["photo_classification_facts"]
    ] == [1, 2]
    assert [
        fact["confirmation_version"]
        for fact in by_id[high["item_id"]]["photo_classification_facts"]
    ] == [1, 2]
    assert by_id[low["item_id"]]["photo_classification_facts"][-1]["human_correction"] is not None
    assert by_id[high["item_id"]]["photo_classification_facts"][-1]["human_correction"] is None

    retried = client.post(
        f"/local/api/upload-batches/{batch_id}/items/{low['item_id']}/photo-category-corrections",
        json={"corrected_category": "real_photo", "reviewer_note": "审核后确认为商品实拍。"},
    )
    assert retried.status_code == 200
    retry_batch = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert all(len(item["photo_classification_facts"]) == 2 for item in retry_batch["items"])

    submitted = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json=_confirm_request(low["item_id"]),
    )
    assert submitted.status_code == 200
    final_batch = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert final_batch["style_test_permission"]["confirmation_version"] == 2
    assert final_batch["style_test_product_confirmation"]["confirmation_version"] == 2
    assert bridge.submit_count == 1


def test_style_handoff_failure_is_a_stable_chinese_conflict_response(tmp_path) -> None:
    from product_engine.api.local_app import create_local_app
    from product_engine.services.style_test_handoff import StyleHandoffFailure

    class RejectingBridge(_CountingBridge):
        async def stage_and_submit(self, photos, build_handoff):  # type: ignore[no-untyped-def]
            raise StyleHandoffFailure("handoff_idempotency_conflict")

    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=RejectingBridge(),
        )
    )
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()["batch"][
        "batch_id"
    ]
    item = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("studio-product.png", _png_bytes(), "image/png")},
    ).json()["item"]

    rejected = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json=_confirm_request(item["item_id"]),
    )

    assert rejected.status_code == 409
    assert rejected.json() == {
        "ok": False,
        "error_code": "handoff_idempotency_conflict",
        "message_zh": "本批商品版本与已有交接记录冲突，请刷新后新建批次。",
    }


def test_bound_batch_correction_uses_global_v3_after_another_batch_reserved_v2(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later correction must not reuse another batch's product/version key."""

    from product_engine.api.local_app import create_local_app
    from product_engine.services.local_upload_batches import LocalUploadBatchStore

    nonce = "task8-global-correction-nonce"
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_TEST_PRODUCT_CATALOG", json.dumps(_style_test_catalog())
    )
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", nonce)
    with _running_task7_fake(nonce) as (base_url, task7):
        monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_URL", base_url)
        client = TestClient(create_local_app(tmp_path))
        store = LocalUploadBatchStore(tmp_path / "upload-batches.json")

        batch_a = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
            "batch"
        ]["batch_id"]
        item_a = client.post(
            f"/local/api/upload-batches/{batch_a}/images",
            data={"position": "0"},
            files={"file": ("ambient-product.png", _png_bytes("white"), "image/png")},
        ).json()["item"]
        batch_b = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
            "batch"
        ]["batch_id"]
        item_b = client.post(
            f"/local/api/upload-batches/{batch_b}/images",
            data={"position": "0"},
            files={"file": ("studio-product.png", _png_bytes("black"), "image/png")},
        ).json()["item"]

        binding = _style_test_catalog()["test-product-turquoise-bracelet"]
        assert store.bind_style_test_product(batch_a, **binding) == 1
        assert (
            store.confirm_style_test_batch(
                batch_a,
                **binding,
                main_photo_id=UUID(item_a["item_id"]),
                transform_permission=True,
            )
            == 1
        )
        assert store.bind_style_test_product(batch_b, **binding) == 2

        corrected = client.post(
            f"/local/api/upload-batches/{batch_a}/items/{item_a['item_id']}/photo-category-corrections",
            json={"corrected_category": "real_photo", "reviewer_note": "审核确认商品实拍。"},
        )
        assert corrected.status_code == 200
        assert corrected.json()["batch_confirmation_version"] == 3
        corrected_batch = client.get(f"/local/api/upload-batches/{batch_a}").json()["batch"]
        assert corrected_batch["style_test_permission"]["confirmation_version"] == 3
        assert corrected_batch["style_test_product_confirmation"]["confirmation_version"] == 3
        assert (
            corrected_batch["style_test_product_confirmation"]["main_photo_id"]
            == (item_a["item_id"])
        )

        submitted_b = client.post(
            f"/local/api/upload-batches/{batch_b}/confirm-and-generate",
            json=_confirm_request(item_b["item_id"]),
        )
        submitted_a = client.post(
            f"/local/api/upload-batches/{batch_a}/confirm-and-generate",
            json=_confirm_request(item_a["item_id"]),
        )
        assert submitted_b.status_code == submitted_a.status_code == 200
        handoffs = task7["handoffs"]
        assert isinstance(handoffs, list)
        assert [handoff["confirmation_version"] for handoff in handoffs] == [2, 3]
        assert [handoff["idempotency_key"] for handoff in handoffs] == [
            "style-test:test-product-turquoise-bracelet:v2",
            "style-test:test-product-turquoise-bracelet:v3",
        ]


def test_move_provisional_v2_into_bound_v1_rebases_target_to_global_v3_and_submits(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move may not leave one target photo at v1 and the incoming photo at v2."""

    from product_engine.api.local_app import create_local_app
    from product_engine.services.local_upload_batches import LocalUploadBatchStore

    nonce = "task8-global-move-nonce"
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_TEST_PRODUCT_CATALOG", json.dumps(_style_test_catalog())
    )
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", nonce)
    with _running_task7_fake(nonce) as (base_url, task7):
        monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_URL", base_url)
        client = TestClient(create_local_app(tmp_path))
        store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
        binding = _style_test_catalog()["test-product-turquoise-bracelet"]

        target_id = client.post("/local/api/upload-batches", json={"expected_count": 2}).json()[
            "batch"
        ]["batch_id"]
        target_item = client.post(
            f"/local/api/upload-batches/{target_id}/images",
            data={"position": "0"},
            files={"file": ("studio-target.png", _png_bytes("white"), "image/png")},
        ).json()["item"]
        assert store.bind_style_test_product(target_id, **binding) == 1

        reserved_id = store.create(1).batch_id
        assert store.bind_style_test_product(reserved_id, **binding) == 2

        source_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
            "batch"
        ]["batch_id"]
        source_item = client.post(
            f"/local/api/upload-batches/{source_id}/images",
            data={"position": "0"},
            files={"file": ("ambient-product.png", _png_bytes("black"), "image/png")},
        ).json()["item"]
        provisional = client.post(
            f"/local/api/upload-batches/{source_id}/items/{source_item['item_id']}/photo-category-corrections",
            json={"corrected_category": "real_photo", "reviewer_note": "审核确认商品实拍。"},
        )
        assert provisional.status_code == 200
        assert provisional.json()["batch_confirmation_version"] == 2

        moved = client.post(
            f"/local/api/upload-batches/{source_id}/items/{source_item['item_id']}/move-to-batch",
            json={
                "target_batch_id": target_id,
                "target_product_id": "test-product-turquoise-bracelet",
            },
        )
        assert moved.status_code == 200
        assert moved.json()["batch_confirmation_version"] == 3
        assert moved.json()["can_confirm"] is True

        target = client.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
        active = [
            item for item in target["items"] if item["status"] in {"pending_review", "reviewed"}
        ]
        assert {
            item["photo_classification_facts"][-1]["confirmation_version"] for item in active
        } == {3}
        assert [
            fact["confirmation_version"] for fact in target["style_test_confirmation_versions"]
        ] == [1, 3]
        source = client.get(f"/local/api/upload-batches/{source_id}").json()["batch"]
        assert source["items"] == []
        assert source["item_audit"][-1]["action"] == "moved"

        submitted = client.post(
            f"/local/api/upload-batches/{target_id}/confirm-and-generate",
            json=_confirm_request(target_item["item_id"]),
        )
        assert submitted.status_code == 200
        confirmed = client.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
        assert confirmed["style_test_permission"]["confirmation_version"] == 3
        assert confirmed["style_test_product_confirmation"]["confirmation_version"] == 3
        handoffs = task7["handoffs"]
        assert isinstance(handoffs, list)
        assert handoffs[-1]["idempotency_key"] == ("style-test:test-product-turquoise-bracelet:v3")


def test_bound_target_move_rebases_all_histories_after_each_bound_payload_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal photo history may not retain an older key after a successful move."""

    from product_engine.api.local_app import create_local_app
    from product_engine.services.local_upload_batches import LocalUploadBatchStore

    nonce = "task8-terminal-history-rebase-nonce"
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_TEST_PRODUCT_CATALOG", json.dumps(_style_test_catalog())
    )
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", nonce)
    with _running_task7_fake(nonce) as (base_url, task7):
        monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_URL", base_url)
        client = TestClient(create_local_app(tmp_path))
        store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
        binding = _style_test_catalog()["test-product-turquoise-bracelet"]

        target_id = client.post("/local/api/upload-batches", json={"expected_count": 3}).json()[
            "batch"
        ]["batch_id"]
        primary = client.post(
            f"/local/api/upload-batches/{target_id}/images",
            data={"position": "0"},
            files={"file": ("studio-primary.png", _png_bytes("white"), "image/png")},
        ).json()["item"]
        failed_history = client.post(
            f"/local/api/upload-batches/{target_id}/images",
            data={"position": "1"},
            files={"file": ("studio-history.png", _png_bytes("blue"), "image/png")},
        ).json()["item"]
        assert store.bind_style_test_product(target_id, **binding) == 1
        store.mark_failed(
            target_id,
            1,
            "studio-history.png",
            failed_history["sha256"],
            "photo_decode_failed",
            replace_existing=True,
        )

        source_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
            "batch"
        ]["batch_id"]
        incoming = client.post(
            f"/local/api/upload-batches/{source_id}/images",
            data={"position": "0"},
            files={"file": ("studio-copy.png", _png_bytes("white"), "image/png")},
        ).json()["item"]
        assert store.bind_style_test_product(source_id, **binding) == 3

        moved = client.post(
            f"/local/api/upload-batches/{source_id}/items/{incoming['item_id']}/move-to-batch",
            json={
                "target_batch_id": target_id,
                "target_product_id": "test-product-turquoise-bracelet",
            },
        )
        assert moved.status_code == 200
        assert moved.json()["batch_confirmation_version"] == 5

        target = client.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
        by_id = {item["item_id"]: item for item in target["items"]}
        assert by_id[primary["item_id"]]["status"] == "pending_review"
        assert by_id[incoming["item_id"]]["status"] == "duplicate"
        assert by_id[failed_history["item_id"]]["status"] == "failed"
        for item in by_id.values():
            facts = item["photo_classification_facts"]
            assert facts[-1]["confirmation_version"] == 5
            assert facts[-1]["automatic_decision"] == facts[0]["automatic_decision"]
            assert facts[-1]["human_correction"] == facts[-2]["human_correction"]
        assert target["item_audit"][-1]["action"] == "moved"

        removed = client.delete(
            f"/local/api/upload-batches/{target_id}/items/{failed_history['item_id']}"
        )
        assert removed.status_code == 200
        assert removed.json()["can_confirm"] is False
        assert "photo_conflict_present" in removed.json()["missing_codes"]
        assert removed.json()["batch_confirmation_version"] == 6
        deduplicated = client.delete(
            f"/local/api/upload-batches/{target_id}/items/{incoming['item_id']}"
        )
        assert deduplicated.status_code == 200
        assert deduplicated.json()["can_confirm"] is True
        assert deduplicated.json()["batch_confirmation_version"] == 7
        recovered = client.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
        assert [audit["action"] for audit in recovered["item_audit"][-3:]] == [
            "moved",
            "removed",
            "removed",
        ]

        submitted = client.post(
            f"/local/api/upload-batches/{target_id}/confirm-and-generate",
            json=_confirm_request(primary["item_id"]),
        )
        assert submitted.status_code == 200
        handoffs = task7["handoffs"]
        assert isinstance(handoffs, list)
        assert handoffs[-1]["idempotency_key"] == ("style-test:test-product-turquoise-bracelet:v7")


def test_move_after_ambiguous_v1_submit_forces_v2_and_retries_without_key_conflict(
    tmp_path,
) -> None:
    """An uncertain v1 delivery must never make changed target facts reuse v1."""

    from product_engine.api.local_app import create_local_app

    bridge = _AmbiguousThenSuccessfulBridge()
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=bridge,
        )
    )
    target_id = client.post("/local/api/upload-batches", json={"expected_count": 2}).json()[
        "batch"
    ]["batch_id"]
    primary = client.post(
        f"/local/api/upload-batches/{target_id}/images",
        data={"position": "0"},
        files={"file": ("studio-primary.png", _png_bytes("white"), "image/png")},
    ).json()["item"]

    ambiguous = client.post(
        f"/local/api/upload-batches/{target_id}/confirm-and-generate",
        json=_confirm_request(primary["item_id"]),
    )
    assert ambiguous.status_code == 503
    assert ambiguous.json()["error_code"] == "style_bridge_ambiguous_delivery"
    assert not (tmp_path / "style-test-handoffs.json").exists()
    after_ambiguous = client.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
    assert after_ambiguous["style_test_product_confirmation"]["confirmation_version"] == 1
    assert after_ambiguous["style_test_permission"]["confirmation_version"] == 1

    source_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
        "batch"
    ]["batch_id"]
    incoming = client.post(
        f"/local/api/upload-batches/{source_id}/images",
        data={"position": "0"},
        files={"file": ("studio-unique.png", _png_bytes("black"), "image/png")},
    ).json()["item"]
    moved = client.post(
        f"/local/api/upload-batches/{source_id}/items/{incoming['item_id']}/move-to-batch",
        json={
            "target_batch_id": target_id,
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert moved.status_code == 200
    assert moved.json()["batch_confirmation_version"] == 2
    moved_batch = client.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
    assert moved_batch["style_test_product_confirmation"]["confirmation_version"] == 2
    assert moved_batch["style_test_permission"]["confirmation_version"] == 2
    assert {
        item["photo_classification_facts"][-1]["confirmation_version"]
        for item in moved_batch["items"]
    } == {2}

    retried = client.post(
        f"/local/api/upload-batches/{target_id}/confirm-and-generate",
        json=_confirm_request(primary["item_id"]),
    )
    assert retried.status_code == 200
    assert retried.json()["current_status"] == "processing"
    assert bridge.keys == [
        "style-test:test-product-turquoise-bracelet:v1",
        "style-test:test-product-turquoise-bracelet:v2",
    ]


def test_ambiguous_bound_source_move_versions_source_and_target_before_both_retry(
    tmp_path,
) -> None:
    """Moving changed bytes must retire the uncertain source key as well as the target key."""

    from product_engine.api.local_app import create_local_app

    bridge = _AmbiguousThenSuccessfulBridge()
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=bridge,
        )
    )
    source_id = client.post("/local/api/upload-batches", json={"expected_count": 2}).json()[
        "batch"
    ]["batch_id"]
    main = client.post(
        f"/local/api/upload-batches/{source_id}/images",
        data={"position": "0"},
        files={"file": ("studio-main.png", _png_bytes("white"), "image/png")},
    ).json()["item"]
    remaining = client.post(
        f"/local/api/upload-batches/{source_id}/images",
        data={"position": "1"},
        files={"file": ("studio-detail.png", _png_bytes("black"), "image/png")},
    ).json()["item"]
    ambiguous = client.post(
        f"/local/api/upload-batches/{source_id}/confirm-and-generate",
        json=_confirm_request(main["item_id"]),
    )
    assert ambiguous.status_code == 503

    target_id = client.post("/local/api/upload-batches", json={"expected_count": 1}).json()[
        "batch"
    ]["batch_id"]
    moved = client.post(
        f"/local/api/upload-batches/{source_id}/items/{main['item_id']}/move-to-batch",
        json={
            "target_batch_id": target_id,
            "target_product_id": "test-product-turquoise-bracelet",
        },
    )
    assert moved.status_code == 200
    assert moved.json()["batch_confirmation_version"] == 3

    source = client.get(f"/local/api/upload-batches/{source_id}").json()["batch"]
    target = client.get(f"/local/api/upload-batches/{target_id}").json()["batch"]
    assert [
        fact["confirmation_version"] for fact in source["style_test_confirmation_versions"]
    ] == [1, 2]
    assert source["items"][0]["item_id"] == remaining["item_id"]
    assert source["items"][0]["photo_classification_facts"][-1]["confirmation_version"] == 2
    assert source["style_test_product_confirmation"] is None
    assert source["style_test_permission"] is None
    assert target["style_test_confirmation_versions"][-1]["confirmation_version"] == 3
    assert target["items"][0]["photo_classification_facts"][-1]["confirmation_version"] == 3

    source_readiness = client.get(f"/local/api/upload-batches/{source_id}/style-readiness").json()
    assert source_readiness["batch_confirmation_version"] == 2
    assert "main_photo_required" in source_readiness["missing_codes"]

    source_retry = client.post(
        f"/local/api/upload-batches/{source_id}/confirm-and-generate",
        json=_confirm_request(remaining["item_id"]),
    )
    target_submit = client.post(
        f"/local/api/upload-batches/{target_id}/confirm-and-generate",
        json=_confirm_request(main["item_id"]),
    )
    assert source_retry.status_code == 200
    assert target_submit.status_code == 200
    assert bridge.keys == [
        "style-test:test-product-turquoise-bracelet:v1",
        "style-test:test-product-turquoise-bracelet:v2",
        "style-test:test-product-turquoise-bracelet:v3",
    ]


def test_ambiguous_bound_delete_then_reupload_advances_twice_and_retries_v3(
    tmp_path,
) -> None:
    """Delete and replacement upload are two payload changes and need two fresh keys."""

    from product_engine.api.local_app import create_local_app

    bridge = _AmbiguousThenSuccessfulBridge()
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=bridge,
        )
    )
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 2}).json()["batch"][
        "batch_id"
    ]
    main = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "0"},
        files={"file": ("studio-main.png", _png_bytes("white"), "image/png")},
    ).json()["item"]
    detail = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "1"},
        files={"file": ("studio-detail.png", _png_bytes("black"), "image/png")},
    ).json()["item"]
    ambiguous = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json=_confirm_request(main["item_id"]),
    )
    assert ambiguous.status_code == 503

    deleted = client.delete(f"/local/api/upload-batches/{batch_id}/items/{detail['item_id']}")
    assert deleted.status_code == 200
    assert deleted.json()["batch_confirmation_version"] == 2
    after_delete = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert after_delete["style_test_product_confirmation"]["confirmation_version"] == 2
    assert after_delete["style_test_permission"]["confirmation_version"] == 2

    replacement = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "1"},
        files={
            "file": (
                "studio-replacement.png",
                _png_bytes("blue"),
                "image/png",
            )
        },
    )
    assert replacement.status_code == 201
    after_reupload = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert after_reupload["style_test_confirmation_versions"][-1]["confirmation_version"] == 3
    assert {
        item["photo_classification_facts"][-1]["confirmation_version"]
        for item in after_reupload["items"]
    } == {3}
    assert after_reupload["style_test_product_confirmation"]["confirmation_version"] == 3
    assert after_reupload["style_test_permission"]["confirmation_version"] == 3

    retried = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json=_confirm_request(main["item_id"]),
    )
    assert retried.status_code == 200
    assert bridge.keys == [
        "style-test:test-product-turquoise-bracelet:v1",
        "style-test:test-product-turquoise-bracelet:v3",
    ]


def test_bound_red_reupload_is_one_atomic_version_and_failed_retry_writes_nothing(
    tmp_path,
) -> None:
    """A red-card replacement either commits once in full or preserves the old card."""

    from product_engine.api.local_app import create_local_app
    from product_engine.services.local_upload_batches import LocalUploadBatchStore

    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog=_style_test_catalog(),
            style_bridge_client=_CountingBridge(),
        )
    )
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
    batch_id = client.post("/local/api/upload-batches", json={"expected_count": 3}).json()["batch"][
        "batch_id"
    ]
    uploaded = []
    for position, color in enumerate(("white", "black", "blue")):
        uploaded.append(
            client.post(
                f"/local/api/upload-batches/{batch_id}/images",
                data={"position": str(position)},
                files={
                    "file": (
                        f"studio-{position}.png",
                        _png_bytes(color),
                        "image/png",
                    )
                },
            ).json()["item"]
        )
    binding = _style_test_catalog()["test-product-turquoise-bracelet"]
    assert store.bind_style_test_product(batch_id, **binding) == 1
    assert (
        store.confirm_style_test_batch(
            batch_id,
            **binding,
            main_photo_id=UUID(uploaded[0]["item_id"]),
            transform_permission=True,
        )
        == 1
    )
    store.mark_failed(
        batch_id,
        1,
        uploaded[1]["filename"],
        uploaded[1]["sha256"],
        "photo_decode_failed",
        replace_existing=True,
    )
    store.mark_failed(
        batch_id,
        2,
        uploaded[2]["filename"],
        uploaded[2]["sha256"],
        "photo_decode_failed",
        replace_existing=True,
    )
    assert store.batch_confirmation_version(batch_id) == 3

    before_failed_retry = (tmp_path / "upload-batches.json").read_bytes()
    failed_retry = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "2"},
        files={"file": ("broken.png", b"not-an-image", "image/png")},
    )
    assert failed_retry.status_code == 422
    assert (tmp_path / "upload-batches.json").read_bytes() == before_failed_retry
    assert store.batch_confirmation_version(batch_id) == 3

    replaced = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": "1"},
        files={
            "file": (
                "studio-recovered.png",
                _png_bytes("green"),
                "image/png",
            )
        },
    )
    assert replaced.status_code == 201
    assert store.batch_confirmation_version(batch_id) == 4
    current = client.get(f"/local/api/upload-batches/{batch_id}").json()["batch"]
    assert current["item_audit"][-1]["action"] == "replaced"
    assert {
        item["photo_classification_facts"][-1]["confirmation_version"]
        for item in current["items"]
        if item["photo_classification_facts"]
    } == {4}
    assert current["style_test_product_confirmation"]["confirmation_version"] == 4
    assert current["style_test_permission"]["confirmation_version"] == 4
