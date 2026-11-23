"""Transport-level safety tests for the removable local style-test bridge."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from product_engine.contracts.style_test_bridge import (
    ClassificationStyleHandoffV1,
    ConfirmedPhotoV1,
    canonical_handoff_bytes,
)

PHOTO_ID = UUID("11111111-1111-4111-8111-111111111111")
RECEIPT_ID = UUID("22222222-2222-4222-8222-222222222222")
HANDOFF_ID = UUID("33333333-3333-4333-8333-333333333333")
JOB_ID = UUID("44444444-4444-4444-8444-444444444444")
CANDIDATE_ID = UUID("55555555-5555-4555-8555-555555555555")
SECOND_CANDIDATE_ID = UUID("66666666-6666-4666-8666-666666666666")
THIRD_CANDIDATE_ID = UUID("77777777-7777-4777-8777-777777777777")
ASSET_ID = UUID("88888888-8888-4888-8888-888888888888")
REVIEW_ID = UUID("99999999-9999-4999-8999-999999999999")
PHOTO_BYTES = b"desensitized-local-image-bytes"
PHOTO_SHA256 = sha256(PHOTO_BYTES).hexdigest()
CANDIDATE_BYTES = b"synthetic-png-candidate"
REAL_BYTES = b"synthetic-jpeg-real-photo"


def _candidate_payload(candidate_id: UUID, sequence_number: int) -> dict[str, object]:
    return {
        "candidate_id": str(candidate_id),
        "sequence_number": sequence_number,
        "content_sha256": sha256(CANDIDATE_BYTES).hexdigest(),
        "mime_type": "image/png",
        "width": 24,
        "height": 18,
        "identity": "style_candidate",
        "content_url": (
            "/v1/local-test/classification-bridge/image-outputs/"
            f"{JOB_ID}/candidates/{candidate_id}/content"
        ),
        "publishable": False,
    }


def _review_job_payload() -> dict[str, object]:
    return {
        "job_id": str(JOB_ID),
        "handoff_id": str(HANDOFF_ID),
        "output_type": "scene",
        "job_status": "awaiting_customer_review",
        "status": "awaiting_review",
        "session_id": str(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")),
        "current_job_id": str(JOB_ID),
        "regeneration_count": 0,
        "candidates": [
            _candidate_payload(CANDIDATE_ID, 1),
            _candidate_payload(SECOND_CANDIDATE_ID, 2),
            _candidate_payload(THIRD_CANDIDATE_ID, 3),
        ],
        "source_product_ids": ["test-product-bridge"],
        "source_asset_ids": [str(ASSET_ID)],
        "approved_candidate_id": None,
        "approved_associations": [],
        "publishable": False,
    }


def test_handoff_output_summaries_preserve_authoritative_types_and_reject_mismatched_ids():
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeProtocolError,
        _parse_bridge_result,
    )

    payload = {
        "handoff_id": str(HANDOFF_ID),
        "status": "awaiting_review",
        "output_job_ids": [str(JOB_ID)],
        "created": False,
        "publishable": False,
        "output_jobs": [
            {
                "job_id": str(JOB_ID),
                "output_type": "real_photo",
                "status": "awaiting_customer_review",
            }
        ],
    }
    parsed = _parse_bridge_result(payload, HANDOFF_ID)
    assert parsed.output_jobs[0].output_type == "real_photo"
    assert parsed.output_jobs[0].job_id == JOB_ID
    payload["output_jobs"][0]["job_id"] = str(ASSET_ID)
    with pytest.raises(StyleBridgeProtocolError):
        _parse_bridge_result(payload, HANDOFF_ID)


def test_review_job_approval_metadata_is_bound_to_its_candidate_product_and_source():
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeProtocolError,
        _parse_review_job,
    )

    payload = _review_job_payload()
    payload.update(
        status="approved",
        job_status="customer_approved",
        approved_candidate_id=str(SECOND_CANDIDATE_ID),
        approved_associations=[
            {
                "product_id": "test-product-bridge",
                "candidate_id": str(SECOND_CANDIDATE_ID),
                "asset_id": str(ASSET_ID),
            }
        ],
    )
    parsed = _parse_review_job(payload, JOB_ID)
    assert parsed.approved_candidate_id == SECOND_CANDIDATE_ID
    assert parsed.approved_associations[0].asset_id == ASSET_ID
    payload["approved_associations"][0]["candidate_id"] = str(CANDIDATE_ID)
    with pytest.raises(StyleBridgeProtocolError):
        _parse_review_job(payload, JOB_ID)


def _approved_image_payload(*, content_url: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "image_kind": "style_image",
        "candidate_id": str(CANDIDATE_ID),
        "identity": "style_candidate",
        "content_sha256": sha256(CANDIDATE_BYTES).hexdigest(),
        "mime_type": "image/png",
        "width": 24,
        "height": 18,
        "customer_approved": True,
        "publishable": False,
        "source_product_ids": ["test-product-bridge"],
        "source_asset_ids": [str(ASSET_ID)],
    }
    if content_url:
        payload["content_url"] = (
            "/v1/local-test/classification-bridge/image-outputs/"
            f"{JOB_ID}/candidates/{CANDIDATE_ID}/content"
        )
    return payload


def _real_image_payload(*, content_url: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "image_kind": "real_photo",
        "asset_id": str(ASSET_ID),
        "identity": "original_real_photo",
        "content_sha256": sha256(REAL_BYTES).hexdigest(),
        "mime_type": "image/jpeg",
        "width": 24,
        "height": 18,
    }
    if content_url:
        payload["content_url"] = (
            f"/v1/local-test/classification-bridge/assets/{ASSET_ID}/content?job_id={JOB_ID}"
        )
    return payload


def _review_decision_payload() -> dict[str, object]:
    return {
        "review_id": str(REVIEW_ID),
        "job_id": str(JOB_ID),
        "candidate_id": str(CANDIDATE_ID),
        "decision": "approved",
        "reason": None,
        "job_status": "customer_approved",
        "candidate_state": "customer_approved",
        "image_kind": "style_image",
        "customer_approved": True,
        "publishable": False,
        "source_product_ids": ["test-product-bridge"],
        "source_asset_ids": [str(ASSET_ID)],
        "product_associations": [
            {
                "product_id": "test-product-bridge",
                "approved_image": _approved_image_payload(content_url=False),
                "real_photo": _real_image_payload(content_url=False),
            }
        ],
        "created_at": "2030-01-01T00:00:00+00:00",
        "session_id": str(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")),
        "next_job_id": None,
        "regeneration_count": 0,
        "status": "approved",
        "outcome": "approved",
        "current_session_status": "approved",
        "current_job_id": str(JOB_ID),
        "current_regeneration_count": 0,
        "suite_state": None,
    }


def test_review_decision_accepts_nullable_suite_state_and_rejects_wrong_type():
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeProtocolError,
        _parse_review_decision,
    )

    payload = _review_decision_payload()
    parsed = _parse_review_decision(payload, JOB_ID, CANDIDATE_ID, "approved", None)
    assert parsed.suite_state is None

    payload["suite_state"] = "completed"
    parsed = _parse_review_decision(payload, JOB_ID, CANDIDATE_ID, "approved", None)
    assert parsed.suite_state == "completed"

    payload["suite_state"] = 1
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_review_decision_invalid"):
        _parse_review_decision(payload, JOB_ID, CANDIDATE_ID, "approved", None)


@pytest.mark.parametrize("terminal", ["approved", "manual_attention_required"])
def test_delayed_negative_acknowledgement_preserves_historical_outcome_and_live_cursor(terminal):
    from product_engine.integrations.style_test_bridge_client import _parse_review_decision

    payload = _review_decision_payload()
    payload.update(
        decision="not_my_style",
        job_status="customer_rejected",
        candidate_state="customer_rejected",
        customer_approved=False,
        product_associations=[],
        status="awaiting_review",
        outcome="regenerated",
        regeneration_count=1,
        next_job_id=str(SECOND_CANDIDATE_ID),
        current_session_status=terminal,
        current_job_id=str(THIRD_CANDIDATE_ID),
        current_regeneration_count=3,
    )
    parsed = _parse_review_decision(payload, JOB_ID, CANDIDATE_ID, "not_my_style", None)
    assert parsed.session_status == "awaiting_review"
    assert parsed.current_session_status == terminal
    assert parsed.current_job_id == THIRD_CANDIDATE_ID


def test_manual_generation_failure_accepts_only_explicit_empty_terminal_job():
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeProtocolError,
        _parse_review_decision,
        _parse_review_job,
    )

    payload = _review_job_payload()
    payload.update(
        status="manual_attention_required",
        job_status="generation_failed",
        regeneration_count=1,
        candidates=[],
    )
    assert _parse_review_job(payload, JOB_ID).candidates == ()
    payload["status"] = "awaiting_review"
    with pytest.raises(StyleBridgeProtocolError):
        _parse_review_job(payload, JOB_ID)
    decision = _review_decision_payload()
    decision.update(
        decision="not_my_style",
        job_status="customer_rejected",
        candidate_state="customer_rejected",
        customer_approved=False,
        product_associations=[],
        status="manual_attention_required",
        outcome="manual_generation_unavailable",
        next_job_id=str(SECOND_CANDIDATE_ID),
        regeneration_count=1,
        current_session_status="manual_attention_required",
        current_job_id=str(SECOND_CANDIDATE_ID),
        current_regeneration_count=1,
    )
    parsed = _parse_review_decision(decision, JOB_ID, CANDIDATE_ID, "not_my_style", None)
    assert parsed.next_job_id == SECOND_CANDIDATE_ID
    assert parsed.outcome == "manual_generation_unavailable"


@pytest.fixture
def loopback_bridge() -> object:
    captured: dict[str, object] = {}
    app = FastAPI()

    def require_nonce(request: Request) -> Response | None:
        if request.headers.get("x-local-classification-bridge-nonce") != "nonce":
            return JSONResponse(status_code=403, content={"detail": "unauthorized"})
        return None

    @app.get("/v1/local-test/classification-bridge/image-outputs/{job_id}")
    async def get_job(job_id: UUID, request: Request) -> Response:
        if failure := require_nonce(request):
            return failure
        assert job_id == JOB_ID
        return JSONResponse(_review_job_payload())

    @app.get(
        "/v1/local-test/classification-bridge/image-outputs/{job_id}/candidates/{candidate_id}/content"
    )
    async def get_candidate(job_id: UUID, candidate_id: UUID, request: Request) -> Response:
        if failure := require_nonce(request):
            return failure
        assert job_id == JOB_ID
        assert candidate_id == CANDIDATE_ID
        return Response(content=CANDIDATE_BYTES, media_type="image/png")

    @app.get("/v1/local-test/classification-bridge/assets/{asset_id}/content")
    async def get_real(asset_id: UUID, job_id: UUID, request: Request) -> Response:
        if failure := require_nonce(request):
            return failure
        assert asset_id == ASSET_ID
        assert job_id == JOB_ID
        return Response(content=REAL_BYTES, media_type="image/jpeg")

    @app.post(
        "/v1/local-test/classification-bridge/image-outputs/{job_id}/candidates/{candidate_id}/review"
    )
    async def review(job_id: UUID, candidate_id: UUID, request: Request) -> Response:
        if failure := require_nonce(request):
            return failure
        assert job_id == JOB_ID
        assert candidate_id == CANDIDATE_ID
        captured["review"] = await request.json()
        return JSONResponse(status_code=201, content=_review_decision_payload())

    @app.get("/v1/local-test/classification-bridge/products/{product_id}/images")
    async def get_product_images(
        product_id: str, handoff_id: UUID, request: Request, view: str = "default"
    ) -> Response:
        if failure := require_nonce(request):
            return failure
        assert product_id == "test-product-bridge"
        assert handoff_id == HANDOFF_ID
        return JSONResponse(
            {
                "product_id": product_id,
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

    return type(
        "LoopbackBridge",
        (),
        {
            "url": "http://style-bridge.test",
            "transport": httpx.ASGITransport(app=app),
            "captured": captured,
        },
    )()


@pytest.mark.asyncio
async def test_review_client_reads_bound_media_and_submits_idempotent_decision(
    loopback_bridge: object,
) -> None:
    """Catches a client that reads media without the job/handoff provenance fence."""
    from product_engine.integrations.style_test_bridge_client import StyleBridgeClient

    client = StyleBridgeClient(
        enabled=True,
        base_url=loopback_bridge.url,
        transport=loopback_bridge.transport,
        session_nonce="nonce",
    )

    job = await client.get_review_job(JOB_ID)
    assert [item.sequence_number for item in job.candidates] == [1, 2, 3]
    candidate = await client.get_candidate_content(JOB_ID, job.candidates[0].candidate_id)
    assert candidate.mime_type == "image/png"
    assert candidate.content == CANDIDATE_BYTES
    real = await client.get_real_content(JOB_ID, ASSET_ID)
    assert real.mime_type == "image/jpeg"
    assert real.content == REAL_BYTES
    result = await client.review_candidate(
        JOB_ID,
        job.candidates[0].candidate_id,
        decision="approved",
        reason=None,
        idempotency_key="approve-1",
    )
    assert result.status == "customer_approved"
    assert result.publishable is False
    images = await client.get_product_images("test-product-bridge", HANDOFF_ID)
    assert images.resolved_view == "style_image"
    assert images.images[0].candidate_id == CANDIDATE_ID
    assert images.images[0].content_job_id == JOB_ID
    assert (
        await client.get_candidate_content(images.images[0].content_job_id, CANDIDATE_ID)
    ).content == CANDIDATE_BYTES
    real_images = await client.get_product_images("test-product-bridge", HANDOFF_ID, view="real")
    assert real_images.images[0].content_job_id == JOB_ID
    assert real_images.images[0].asset_id == ASSET_ID
    assert (
        await client.get_real_content(real_images.images[0].content_job_id, ASSET_ID)
    ).content == REAL_BYTES
    assert loopback_bridge.captured["review"] == {
        "decision": "approved",
        "reason": None,
        "idempotency_key": "approve-1",
    }


@pytest.mark.asyncio
async def test_review_client_rejects_bad_nonce_redirects_oversized_media_and_leaky_json() -> None:
    """Catches accepting an untrusted server, response, or hidden filesystem metadata."""
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeProtocolError,
    )

    bad_nonce = FastAPI()

    @bad_nonce.get("/v1/local-test/classification-bridge/image-outputs/{job_id}")
    async def unauthorized(job_id: UUID) -> Response:
        return Response(status_code=403)

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=bad_nonce),
        session_nonce="wrong",
    )
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_response_invalid"):
        await client.get_review_job(JOB_ID)

    redirect = FastAPI()

    @redirect.get(
        "/v1/local-test/classification-bridge/image-outputs/{job_id}/candidates/{candidate_id}/content"
    )
    async def redirected(job_id: UUID, candidate_id: UUID) -> Response:
        return Response(status_code=307, headers={"Location": "http://example.test/image"})

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=redirect),
        session_nonce="nonce",
    )
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_redirect_rejected"):
        await client.get_candidate_content(JOB_ID, CANDIDATE_ID)

    large_media = FastAPI()

    @large_media.get(
        "/v1/local-test/classification-bridge/image-outputs/{job_id}/candidates/{candidate_id}/content"
    )
    async def oversized(job_id: UUID, candidate_id: UUID) -> Response:
        return Response(content=b"x" * 5, media_type="image/png")

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=large_media),
        session_nonce="nonce",
        max_media_bytes=4,
    )
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_response_too_large"):
        await client.get_candidate_content(JOB_ID, CANDIDATE_ID)

    leaky_json = FastAPI()

    @leaky_json.get("/v1/local-test/classification-bridge/image-outputs/{job_id}")
    async def leaked(job_id: UUID) -> Response:
        payload = _review_job_payload()
        payload["artifact_path"] = "/private/customer/photo.png"
        payload["publishable"] = True
        return JSONResponse(payload)

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=leaky_json),
        session_nonce="nonce",
    )
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_review_job_invalid"):
        await client.get_review_job(JOB_ID)


@pytest.mark.asyncio
async def test_review_client_accepts_only_whitelisted_fastapi_detail_errors() -> None:
    """Catches treating arbitrary FastAPI detail text as a retryable consumer code."""
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeProtocolError,
        StyleBridgeRemoteError,
    )

    app = FastAPI()

    @app.post(
        "/v1/local-test/classification-bridge/image-outputs/{job_id}/candidates/{candidate_id}/review"
    )
    async def detail_error(job_id: UUID, candidate_id: UUID, request: Request) -> Response:
        return JSONResponse(
            status_code=request.app.state.error_status,
            content={"detail": request.app.state.error_code},
        )

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=app),
        session_nonce="nonce",
    )
    for status_code, code, retryable in (
        (409, "review_generation_in_progress", True),
        (503, "local_review_content_unavailable", True),
    ):
        app.state.error_status = status_code
        app.state.error_code = code
        with pytest.raises(StyleBridgeRemoteError) as raised:
            await client.review_candidate(
                JOB_ID,
                CANDIDATE_ID,
                decision="approved",
                reason=None,
                idempotency_key=f"detail-{status_code}",
            )
        assert raised.value.code == code
        assert raised.value.retryable is retryable

    app.state.error_status = 409
    app.state.error_code = "consumer_invented_error"
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_response_invalid"):
        await client.review_candidate(
            JOB_ID,
            CANDIDATE_ID,
            decision="approved",
            reason=None,
            idempotency_key="detail-unknown",
        )
    app.state.error_status = 409
    app.state.error_code = "local_review_content_unavailable"
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_response_invalid"):
        await client.review_candidate(
            JOB_ID,
            CANDIDATE_ID,
            decision="approved",
            reason=None,
            idempotency_key="detail-wrong-status",
        )


@pytest.mark.asyncio
async def test_review_detail_errors_are_scoped_to_their_exact_endpoint() -> None:
    """Catches a review failure from one endpoint being trusted by a sibling."""
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeProtocolError,
        StyleBridgeRemoteError,
    )

    app = FastAPI()

    def conflict() -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "review_idempotency_conflict"},
        )

    @app.get("/v1/local-test/classification-bridge/image-outputs/{job_id}")
    async def review_job_error(job_id: UUID) -> Response:
        return conflict()

    @app.get(
        "/v1/local-test/classification-bridge/image-outputs/"
        "{job_id}/candidates/{candidate_id}/content"
    )
    async def candidate_content_error(job_id: UUID, candidate_id: UUID) -> Response:
        return conflict()

    @app.get("/v1/local-test/classification-bridge/assets/{asset_id}/content")
    async def real_content_error(asset_id: UUID, job_id: UUID) -> Response:
        return conflict()

    @app.post(
        "/v1/local-test/classification-bridge/image-outputs/"
        "{job_id}/candidates/{candidate_id}/review"
    )
    async def review_candidate_error(job_id: UUID, candidate_id: UUID) -> Response:
        return conflict()

    @app.get("/v1/local-test/classification-bridge/products/{product_id}/images")
    async def product_images_error(product_id: str, handoff_id: UUID) -> Response:
        return conflict()

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=app),
        session_nonce="nonce",
    )
    for request in (
        lambda: client.get_review_job(JOB_ID),
        lambda: client.get_candidate_content(JOB_ID, CANDIDATE_ID),
        lambda: client.get_real_content(JOB_ID, ASSET_ID),
        lambda: client.get_product_images("test-product-bridge", HANDOFF_ID),
    ):
        with pytest.raises(StyleBridgeProtocolError, match="style_bridge_response_invalid"):
            await request()

    with pytest.raises(StyleBridgeRemoteError) as raised:
        await client.review_candidate(
            JOB_ID,
            CANDIDATE_ID,
            decision="approved",
            reason=None,
            idempotency_key="exact-review-error-scope",
        )
    assert (raised.value.code, raised.value.retryable) == (
        "review_idempotency_conflict",
        False,
    )


@pytest.mark.asyncio
async def test_review_client_rejects_mismatched_intent_states_and_untrusted_media_or_dtos() -> None:
    """Catches a rejection response being read as approval, or untrusted DTO/media data."""
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeProtocolError,
    )

    app = FastAPI()

    @app.post(
        "/v1/local-test/classification-bridge/image-outputs/{job_id}/candidates/{candidate_id}/review"
    )
    async def mismatched_review(job_id: UUID, candidate_id: UUID) -> Response:
        payload = _review_decision_payload()
        if getattr(app.state, "review_mode", "mismatch") == "leaky":
            payload["nonce"] = "leaked"
            payload["status"] = "consumer_invented_session_state"
        else:
            payload.update(
                decision="rejected",
                job_status="customer_rejected",
                candidate_state="customer_rejected",
                customer_approved=False,
                product_associations=[],
                status="manual_attention_required",
            )
        return JSONResponse(payload)

    @app.get(
        "/v1/local-test/classification-bridge/image-outputs/{job_id}/candidates/{candidate_id}/content"
    )
    async def invalid_media(job_id: UUID, candidate_id: UUID) -> Response:
        if candidate_id == SECOND_CANDIDATE_ID:
            return Response(content=b"", media_type="image/png")
        return Response(content=b"not-an-image", media_type="text/plain")

    @app.get("/v1/local-test/classification-bridge/image-outputs/{job_id}")
    async def invalid_job(job_id: UUID) -> Response:
        payload = _review_job_payload()
        payload["output_type"] = "consumer_invented_type"
        payload["job_status"] = "consumer_invented_status"
        return JSONResponse(payload)

    @app.get("/v1/local-test/classification-bridge/products/{product_id}/images")
    async def invalid_product(product_id: str, handoff_id: UUID) -> Response:
        payload = {
            "product_id": product_id,
            "requested_view": "default",
            "resolved_view": "consumer_invented_view",
            "images": [],
            "publishable": False,
            "nonce": "leaked",
        }
        return JSONResponse(payload)

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=app),
        session_nonce="nonce",
    )
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_review_decision_invalid"):
        await client.review_candidate(
            JOB_ID,
            CANDIDATE_ID,
            decision="approved",
            reason=None,
            idempotency_key="must-not-turn-into-approval",
        )
    app.state.review_mode = "leaky"
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_review_decision_invalid"):
        await client.review_candidate(
            JOB_ID,
            CANDIDATE_ID,
            decision="approved",
            reason=None,
            idempotency_key="must-not-accept-review-extra",
        )
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_media_type_invalid"):
        await client.get_candidate_content(JOB_ID, CANDIDATE_ID)
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_media_size_invalid"):
        await client.get_candidate_content(JOB_ID, SECOND_CANDIDATE_ID)
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_review_job_invalid"):
        await client.get_review_job(JOB_ID)
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_product_images_invalid"):
        await client.get_product_images("test-product-bridge", HANDOFF_ID)


def confirmed_photo() -> object:
    from product_engine.integrations.style_test_bridge_client import ConfirmedLocalPhoto

    return ConfirmedLocalPhoto(
        photo_id=PHOTO_ID,
        content_sha256=PHOTO_SHA256,
        mime_type="image/jpeg",
        width=24,
        height=18,
        open_content=lambda: BytesIO(PHOTO_BYTES),
    )


def handoff(receipt_id: UUID = RECEIPT_ID) -> ClassificationStyleHandoffV1:
    return ClassificationStyleHandoffV1(
        schema_version="classification-style-handoff-v1",
        handoff_id=HANDOFF_ID,
        customer_test_id="test-customer-bridge",
        product_id="test-product-bridge",
        product_category="turquoise",
        product_form="bracelet",
        classification_version="classification-v1",
        confirmation_version=1,
        main_photo_id=PHOTO_ID,
        transform_permission=True,
        style_generation_allowed=True,
        photos=(
            ConfirmedPhotoV1(
                photo_id=PHOTO_ID,
                content_sha256=PHOTO_SHA256,
                mime_type="image/jpeg",
                width=24,
                height=18,
                automatic_category="real_photo",
                confidence="high",
                human_corrected=False,
                confirmed_category="real_photo",
                media_receipt_id=receipt_id,
            ),
        ),
        idempotency_key="style-test:test-product-bridge:v1",
        desensitized_test_only=True,
        source="classification_local_page",
        publishable=False,
        production_eligible=False,
    )


def fake_consumer(captured: dict[str, object], *, handoff_status: int = 201) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "opc-style-agent"}

    @app.post("/v1/local-test/classification-bridge/media")
    async def stage(request: Request) -> JSONResponse:
        body = await request.body()
        captured.setdefault("media", []).append({"headers": dict(request.headers), "body": body})
        return JSONResponse(
            status_code=201,
            content={
                "receipt_id": str(RECEIPT_ID),
                "photo_id": request.headers["x-photo-id"],
                "content_sha256": request.headers["x-content-sha256"],
                "mime_type": "image/jpeg",
                "width": 24,
                "height": 18,
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        )

    @app.post("/v1/local-test/classification-bridge/handoffs")
    async def accept(request: Request) -> Response:
        captured["handoff"] = await request.body()
        if handoff_status != 201:
            code = (
                "classification_handoff_cleanup_pending"
                if handoff_status == 503
                else "classification_handoff_in_progress"
            )
            return JSONResponse(
                status_code=handoff_status,
                content={"error": {"code": code, "retryable": True}},
            )
        return JSONResponse(
            status_code=201,
            content={
                "handoff_id": str(HANDOFF_ID),
                "status": "processing",
                "output_job_ids": [],
                "created": True,
                "publishable": False,
            },
        )

    return app


@pytest.mark.asyncio
async def test_stages_owned_bytes_before_submitting_canonical_handoff() -> None:
    from product_engine.integrations.style_test_bridge_client import StyleBridgeClient

    captured: dict[str, object] = {}
    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=fake_consumer(captured)),
        session_nonce="test-bridge-nonce",
    )

    result = await client.stage_and_submit(
        [confirmed_photo()], lambda receipts: handoff(receipts[PHOTO_ID].receipt_id)
    )

    assert result.handoff_id == HANDOFF_ID
    assert result.created is True
    media = captured["media"]
    assert isinstance(media, list)
    assert media[0]["body"] == PHOTO_BYTES
    assert media[0]["headers"]["x-local-classification-bridge-nonce"] == "test-bridge-nonce"
    assert media[0]["headers"]["x-content-sha256"] == PHOTO_SHA256
    assert captured["handoff"] == canonical_handoff_bytes(handoff())
    assert b"test-bridge-nonce" not in captured["handoff"]
    assert b"object_key" not in captured["handoff"]
    assert b"source_path" not in captured["handoff"]


@pytest.mark.asyncio
async def test_replays_consumer_receipts_and_preserves_consumer_result() -> None:
    from product_engine.integrations.style_test_bridge_client import StyleBridgeClient

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=fake_consumer({})),
        session_nonce="test-bridge-nonce",
    )

    first = await client.stage_media(confirmed_photo())
    second = await client.stage_media(confirmed_photo())
    result = await client.submit_handoff(handoff(first.receipt_id))

    assert first == second
    assert result.status == "processing"
    assert result.publishable is False


@pytest.mark.asyncio
async def test_rejects_an_unknown_consumer_handoff_status_with_a_stable_code() -> None:
    """Catches treating a newly invented consumer state as a trusted bridge result."""

    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeProtocolError,
    )

    app = FastAPI()

    @app.post("/v1/local-test/classification-bridge/handoffs")
    async def unknown_status() -> JSONResponse:
        return JSONResponse(
            status_code=201,
            content={
                "handoff_id": str(HANDOFF_ID),
                "status": "consumer_added_a_new_status",
                "output_job_ids": [],
                "created": True,
                "publishable": False,
            },
        )

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=app),
        session_nonce="test-bridge-nonce",
    )

    with pytest.raises(
        StyleBridgeProtocolError,
        match="style_bridge_handoff_status_invalid",
    ):
        await client.submit_handoff(handoff())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:not-a-port",
        "https://127.0.0.1:8000",
        "http://192.168.1.2:8000",
    ],
)
async def test_rejects_every_non_numeric_loopback_destination(url: str) -> None:
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeConfigurationError,
    )

    with pytest.raises(StyleBridgeConfigurationError, match="style_bridge_loopback_required"):
        StyleBridgeClient(enabled=True, base_url=url)


@pytest.mark.asyncio
async def test_disabled_client_never_calls_the_consumer() -> None:
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeDisabledError,
    )

    client = StyleBridgeClient(enabled=False, base_url="http://127.0.0.1:8017")

    with pytest.raises(StyleBridgeDisabledError, match="local_classification_bridge_disabled"):
        await client.health()


def test_rejects_custom_transports_and_unsafe_asgi_origins() -> None:
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeConfigurationError,
    )

    app = FastAPI()
    with pytest.raises(StyleBridgeConfigurationError, match="style_bridge_transport_rejected"):
        StyleBridgeClient(
            enabled=True,
            base_url="http://127.0.0.1:8017",
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            session_nonce="test-bridge-nonce",
        )
    for unsafe_origin in (
        "http://user@style-bridge.test",
        "http://style-bridge.test?proxy=true",
        "http://style-bridge.test#fragment",
    ):
        with pytest.raises(StyleBridgeConfigurationError, match="style_bridge_loopback_required"):
            StyleBridgeClient(
                enabled=True,
                base_url=unsafe_origin,
                transport=httpx.ASGITransport(app=app),
                session_nonce="test-bridge-nonce",
            )


@pytest.mark.asyncio
async def test_real_transport_path_disables_proxy_environment_and_uses_numeric_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import product_engine.integrations.style_test_bridge_client as bridge_client

    captured: dict[str, object] = {}

    class ResponseContext:
        async def __aenter__(self) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "ok", "service": "opc-style-agent"},
                request=httpx.Request("GET", "http://127.0.0.1:8017/health"),
            )

        async def __aexit__(self, *args: object) -> None:
            return None

    class CapturingClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client"] = kwargs

        async def __aenter__(self) -> CapturingClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(self, *args: object, **kwargs: object) -> ResponseContext:
            captured["request"] = (args, kwargs)
            return ResponseContext()

    monkeypatch.setenv("HTTP_PROXY", "http://not-a-local-proxy.invalid:8080")
    monkeypatch.setattr(bridge_client.httpx, "AsyncClient", CapturingClient)
    client = bridge_client.StyleBridgeClient(
        enabled=True,
        base_url="http://127.0.0.1:8017",
        session_nonce="test-bridge-nonce",
    )

    assert await client.health() is True
    assert captured["client"] == {
        "transport": None,
        "timeout": httpx.Timeout(5.0),
        "follow_redirects": False,
        "trust_env": False,
    }
    request_args = captured["request"]
    assert isinstance(request_args, tuple)
    assert request_args[0] == ("GET", "http://127.0.0.1:8017/health")
    assert request_args[1]["headers"] == {
        "X-Local-Classification-Bridge-Nonce": "test-bridge-nonce"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "code"),
    [(409, "classification_handoff_in_progress"), (503, "classification_handoff_cleanup_pending")],
)
async def test_preserves_stable_consumer_errors(status_code: int, code: str) -> None:
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeRemoteError,
    )

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=fake_consumer({}, handoff_status=status_code)),
        session_nonce="test-bridge-nonce",
    )

    with pytest.raises(StyleBridgeRemoteError) as raised:
        await client.submit_handoff(handoff())

    assert raised.value.code == code
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_preserves_only_the_legacy_errors_allowed_by_each_bridge_endpoint() -> None:
    """Catches an old receipt or handoff error becoming protocol-invalid or crossing endpoints."""
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeProtocolError,
        StyleBridgeRemoteError,
    )

    app = FastAPI()

    def error_response(request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=request.app.state.error_status,
            content={
                "error": {
                    "code": request.app.state.error_code,
                    "retryable": request.app.state.retryable,
                }
            },
        )

    @app.post("/v1/local-test/classification-bridge/media")
    async def stage_error(request: Request) -> Response:
        return error_response(request)

    @app.post("/v1/local-test/classification-bridge/handoffs")
    async def handoff_error(request: Request) -> Response:
        return error_response(request)

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=app),
        session_nonce="nonce",
    )
    for status_code, code, retryable in (
        (503, "local_media_receipt_expired", True),
        (503, "local_classification_bridge_storage_unavailable", True),
        (422, "classification_handoff_invalid", False),
        (503, "local_style_scene_pin_required", True),
    ):
        app.state.error_status = status_code
        app.state.error_code = code
        app.state.retryable = retryable
        with pytest.raises(StyleBridgeRemoteError) as raised:
            await client.submit_handoff(handoff())
        assert (raised.value.code, raised.value.retryable) == (code, retryable)

    for code in (
        "local_media_manifest_invalid",
        "local_media_manifest_version_unsupported",
        "local_media_storage_unavailable",
    ):
        app.state.error_status = 409
        app.state.error_code = code
        app.state.retryable = False
        with pytest.raises(StyleBridgeRemoteError) as raised:
            await client.stage_media(confirmed_photo())
        assert (raised.value.code, raised.value.retryable) == (code, False)

    app.state.error_status = 503
    app.state.error_code = "local_style_scene_pin_required"
    app.state.retryable = True
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_response_invalid"):
        await client.stage_media(confirmed_photo())


@pytest.mark.asyncio
async def test_rejects_redirects_and_oversized_responses_without_retry() -> None:
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeProtocolError,
    )

    redirect_app = FastAPI()

    @redirect_app.get("/health")
    async def redirect() -> Response:
        return Response(status_code=307, headers={"Location": "http://example.test/health"})

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=redirect_app),
        session_nonce="test-bridge-nonce",
    )
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_redirect_rejected"):
        await client.health()

    large_app = FastAPI()

    @large_app.get("/health")
    async def too_large() -> Response:
        return Response(content=b"x" * 128)

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=large_app),
        session_nonce="test-bridge-nonce",
        max_response_bytes=64,
    )
    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_response_too_large"):
        await client.health()


@pytest.mark.asyncio
async def test_rejects_a_single_oversized_stream_chunk_before_buffering_or_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import product_engine.integrations.style_test_bridge_client as bridge_client

    app = FastAPI()

    @app.get("/health")
    async def oversized_stream() -> StreamingResponse:
        async def body():  # type: ignore[no-untyped-def]
            yield b"x" * 128

        return StreamingResponse(body(), media_type="application/json")

    class GuardedBytearray:
        def __init__(self) -> None:
            self.length = 0

        def __len__(self) -> int:
            return self.length

        def extend(self, chunk: bytes) -> None:
            pytest.fail("oversized response chunk was copied into the accumulator")

    monkeypatch.setattr(bridge_client, "bytearray", GuardedBytearray, raising=False)
    monkeypatch.setattr(
        bridge_client,
        "_json_object",
        lambda response: pytest.fail("oversized response reached downstream parsing"),
    )
    client = bridge_client.StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=app),
        session_nonce="test-bridge-nonce",
        max_response_bytes=64,
    )

    with pytest.raises(
        bridge_client.StyleBridgeProtocolError,
        match="style_bridge_response_too_large",
    ):
        await client.health()


@pytest.mark.asyncio
async def test_rejects_owned_content_hash_mismatch_before_any_network_request() -> None:
    from product_engine.integrations.style_test_bridge_client import (
        ConfirmedLocalPhoto,
        StyleBridgeClient,
        StyleBridgeProtocolError,
    )

    app = FastAPI()

    invalid_photo = ConfirmedLocalPhoto(
        photo_id=PHOTO_ID,
        content_sha256="0" * 64,
        mime_type="image/jpeg",
        width=24,
        height=18,
        open_content=lambda: BytesIO(PHOTO_BYTES),
    )
    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=app),
        session_nonce="test-bridge-nonce",
    )

    with pytest.raises(StyleBridgeProtocolError, match="style_bridge_media_hash_mismatch"):
        await client.stage_media(invalid_photo)


@pytest.mark.asyncio
async def test_partial_staging_does_not_retry_or_submit_a_partial_handoff() -> None:
    from product_engine.integrations.style_test_bridge_client import (
        ConfirmedLocalPhoto,
        StyleBridgeClient,
        StyleBridgeRemoteError,
    )

    calls: list[str] = []
    app = FastAPI()

    @app.post("/v1/local-test/classification-bridge/media")
    async def unavailable_media(request: Request) -> JSONResponse:
        calls.append("media")
        if len(calls) != 2:
            return JSONResponse(
                status_code=201,
                content={
                    "receipt_id": str(RECEIPT_ID),
                    "photo_id": request.headers["x-photo-id"],
                    "content_sha256": request.headers["x-content-sha256"],
                    "mime_type": "image/jpeg",
                    "width": 24,
                    "height": 18,
                    "expires_at": "2030-01-01T00:00:00+00:00",
                },
            )
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "local_media_receipt_unavailable", "retryable": True}},
        )

    @app.post("/v1/local-test/classification-bridge/handoffs")
    async def unexpected_handoff() -> JSONResponse:
        calls.append("handoff")
        return JSONResponse(status_code=500, content={})

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=app),
        session_nonce="test-bridge-nonce",
    )
    second_photo = ConfirmedLocalPhoto(
        photo_id=UUID("44444444-4444-4444-8444-444444444444"),
        content_sha256=PHOTO_SHA256,
        mime_type="image/jpeg",
        width=24,
        height=18,
        open_content=lambda: BytesIO(PHOTO_BYTES),
    )
    with pytest.raises(StyleBridgeRemoteError, match="local_media_receipt_unavailable"):
        await client.stage_and_submit([confirmed_photo(), second_photo], lambda receipts: handoff())

    replayed_receipt = await client.stage_media(confirmed_photo())
    assert replayed_receipt.receipt_id == RECEIPT_ID
    assert calls == ["media", "media", "media"]


@pytest.mark.asyncio
async def test_timeout_and_proxy_environment_do_not_escape_the_in_process_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from product_engine.integrations.style_test_bridge_client import (
        StyleBridgeClient,
        StyleBridgeTransportError,
    )

    slow = FastAPI()

    @slow.get("/health")
    async def slow_health() -> dict[str, str]:
        await asyncio.sleep(0.05)
        return {"status": "ok", "service": "opc-style-agent"}

    monkeypatch.setenv("HTTP_PROXY", "http://not-a-local-proxy.invalid:8080")
    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=slow),
        timeout_seconds=0.001,
        session_nonce="test-bridge-nonce",
    )
    with pytest.raises(StyleBridgeTransportError, match="style_bridge_timeout"):
        await client.health()


@pytest.mark.asyncio
async def test_handoff_submission_uses_its_long_running_timeout() -> None:
    """Generation may exceed the short timeout used by health and media reads."""

    from product_engine.integrations.style_test_bridge_client import StyleBridgeClient

    slow = FastAPI()

    @slow.post("/v1/local-test/classification-bridge/handoffs")
    async def slow_handoff() -> JSONResponse:
        await asyncio.sleep(0.03)
        submitted = handoff()
        return JSONResponse(
            status_code=201,
            content={
                "handoff_id": str(submitted.handoff_id),
                "status": "awaiting_review",
                "output_job_ids": [],
                "created": True,
                "publishable": False,
            },
        )

    client = StyleBridgeClient(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=httpx.ASGITransport(app=slow),
        timeout_seconds=0.001,
        handoff_timeout_seconds=1.0,
        session_nonce="test-bridge-nonce",
    )

    result = await client.submit_handoff(handoff())

    assert result.status == "awaiting_review"
