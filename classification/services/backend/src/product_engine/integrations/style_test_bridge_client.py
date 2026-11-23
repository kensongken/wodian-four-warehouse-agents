"""Loopback-only producer client for the removable classification style bridge.

The adapter accepts an owned file handle supplier, never a filesystem path.  It
does not consult DNS, proxy environment variables, or redirects: the regular
transport has exactly one legal destination, a numeric IPv4 loopback address.
``ASGITransport`` is allowed solely for in-process tests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, BinaryIO, Literal, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx

from product_engine.contracts.style_test_bridge import (
    ClassificationStyleHandoffV1,
    canonical_handoff_bytes,
)

_BRIDGE_PREFIX = "/v1/local-test/classification-bridge"
_ASGI_TEST_ORIGIN = "http://style-bridge.test"
_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_HANDOFF_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024
_DEFAULT_MAX_MEDIA_BYTES = 10 * 1024 * 1024

BridgeStatus = Literal["processing", "needs_confirmation", "pending_real_model", "awaiting_review"]
_BRIDGE_STATUSES = frozenset(
    {"processing", "needs_confirmation", "pending_real_model", "awaiting_review"}
)
ReviewDecision = Literal["approved", "not_my_style", "product_detail_inaccurate", "rejected"]
ReviewOutcomeStatus = Literal["customer_approved", "customer_rejected"]
ProductView = Literal["default", "real"]
ResolvedProductView = Literal["style_image", "real_photo"]
BridgeImageMimeType = Literal["image/jpeg", "image/png"]
BridgeImageKind = Literal["real_photo", "style_image"]

_REVIEW_DECISIONS = frozenset({"approved", "not_my_style", "product_detail_inaccurate", "rejected"})
_REVIEW_OUTCOME_STATUSES = frozenset({"customer_approved", "customer_rejected"})
_REVIEW_SESSION_STATUSES = frozenset(
    {"awaiting_review", "regenerating", "approved", "manual_attention_required"}
)
_PRODUCT_VIEWS = frozenset({"default", "real"})
_RESOLVED_PRODUCT_VIEWS = frozenset({"style_image", "real_photo"})
_BRIDGE_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
_BRIDGE_IMAGE_KINDS = frozenset({"real_photo", "style_image"})
_REVIEW_OUTPUT_TYPES = frozenset({"scene", "multi_product", "real_photo"})
_REVIEW_JOB_STATUSES = frozenset(
    {
        "queued",
        "generating",
        "machine_check",
        "awaiting_customer_review",
        "authenticity_failed",
        "customer_approved",
        "customer_rejected",
        "insufficient_information",
        "permission_unknown",
        "disabled",
        "generation_failed",
        "cleanup_pending",
        "cleanup_abandoned",
    }
)


def _error_specs(status_code: int, retryable: bool, *codes: str) -> dict[str, tuple[int, bool]]:
    return {code: (status_code, retryable) for code in codes}


_BRIDGE_ACCESS_ERRORS = {
    **_error_specs(401, False, "local_classification_bridge_nonce_required"),
    **_error_specs(
        403,
        False,
        "local_classification_bridge_nonce_invalid",
        "local_classification_bridge_loopback_required",
    ),
}
_STAGE_MEDIA_ERRORS = {
    **_BRIDGE_ACCESS_ERRORS,
    **_error_specs(
        503,
        True,
        "local_media_receipt_unavailable",
    ),
    **_error_specs(
        409,
        False,
        "local_media_photo_id_invalid",
        "local_media_hash_invalid",
        "local_media_hash_mismatch",
        "local_media_decode_invalid",
        "local_media_decode_limits_exceeded",
        "local_media_mime_unsupported",
        "local_media_receipt_pending",
        "local_media_receipt_conflict",
        "local_media_receipt_record_failed",
        "local_media_manifest_invalid",
        "local_media_manifest_version_unsupported",
        "local_media_storage_unavailable",
        "local_media_storage_unsafe",
        "local_media_too_large",
        "local_media_read_failed",
        "local_media_write_failed",
        "local_media_stage_closed",
        "local_classification_bridge_disabled",
    ),
}
_HANDOFF_ERRORS = {
    **_BRIDGE_ACCESS_ERRORS,
    **_error_specs(422, False, "classification_handoff_invalid"),
    **_error_specs(
        409,
        False,
        "classification_handoff_idempotency_conflict",
        "classification_handoff_real_asset_missing",
        "local_media_handoff_id_invalid",
        "local_media_receipt_batch_invalid",
        "local_media_receipt_replayed",
        "local_media_manifest_invalid",
        "local_media_manifest_version_unsupported",
        "local_media_storage_unavailable",
        "local_media_storage_unsafe",
        "local_media_stage_closed",
        "local_classification_bridge_disabled",
    ),
    **_error_specs(
        503,
        True,
        "classification_handoff_cleanup_pending",
        "local_classification_bridge_storage_unavailable",
        "local_media_receipt_unavailable",
        "local_media_receipt_expired",
        "local_media_receipt_mismatch",
        "local_media_receipt_decode_mismatch",
        "local_style_scene_pin_required",
        "local_multi_product_sources_required",
    ),
    **_error_specs(409, True, "classification_handoff_in_progress"),
}
_HANDOFF_READ_ERRORS = {
    **_BRIDGE_ACCESS_ERRORS,
    **_error_specs(409, True, "classification_handoff_in_progress"),
    **_error_specs(503, True, "classification_handoff_cleanup_pending"),
}
_REVIEW_JOB_ERRORS = {
    **_error_specs(503, True, "local_review_content_unavailable"),
    **_error_specs(
        409,
        False,
        "review_job_not_awaiting_customer",
        "review_session_product_conflict",
        "review_session_handoff_conflict",
    ),
}
_CANDIDATE_CONTENT_ERRORS = {
    **_error_specs(503, True, "local_review_content_unavailable"),
}
_REAL_CONTENT_ERRORS = {
    **_error_specs(503, True, "local_review_content_unavailable"),
}
_REVIEW_CANDIDATE_ERRORS = {
    **_REVIEW_JOB_ERRORS,
    **_error_specs(409, True, "review_generation_in_progress"),
    **_error_specs(
        409,
        False,
        "review_idempotency_conflict",
        "candidate_review_already_decided",
        "invalid_review_decision",
        "review_candidate_not_current",
        "review_session_already_closed",
        "review_job_already_bound",
    ),
}
_PRODUCT_IMAGES_ERRORS = {
    **_error_specs(503, True, "local_review_content_unavailable"),
}


class StyleBridgeError(RuntimeError):
    """Base error with a stable code safe to present to the local page."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StyleBridgeDisabledError(StyleBridgeError):
    pass


class StyleBridgeConfigurationError(StyleBridgeError):
    pass


class StyleBridgeProtocolError(StyleBridgeError):
    pass


class StyleBridgeTransportError(StyleBridgeError):
    pass


class StyleBridgeRemoteError(StyleBridgeError):
    """A consumer error whose stable retry semantics are preserved verbatim."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class ConfirmedLocalPhoto:
    """Confirmed batch-store metadata plus a producer-owned readable handle.

    ``open_content`` intentionally exposes only a handle.  Object keys and
    source paths remain inside the producer storage adapter and cannot cross
    the handoff boundary.
    """

    photo_id: UUID
    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png", "image/heic"]
    width: int
    height: int
    open_content: Callable[[], BinaryIO]


@dataclass(frozen=True)
class MediaReceipt:
    receipt_id: UUID
    photo_id: UUID
    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png", "image/heic"]
    width: int
    height: int
    expires_at: datetime


@dataclass(frozen=True)
class BridgeJobSummary:
    job_id: UUID
    output_type: str
    status: str


@dataclass(frozen=True)
class BridgeResult:
    handoff_id: UUID
    status: BridgeStatus
    output_job_ids: tuple[UUID, ...]
    created: bool
    publishable: Literal[False]
    output_jobs: tuple[BridgeJobSummary, ...] = ()


@dataclass(frozen=True)
class BridgeImage:
    content: bytes
    mime_type: BridgeImageMimeType


@dataclass(frozen=True)
class ReviewCandidate:
    candidate_id: UUID
    sequence_number: int
    content_sha256: str
    mime_type: BridgeImageMimeType
    width: int
    height: int
    identity: str


@dataclass(frozen=True)
class ApprovedReviewAssociation:
    product_id: str
    candidate_id: UUID
    asset_id: UUID


@dataclass(frozen=True)
class ReviewJob:
    job_id: UUID
    handoff_id: UUID
    output_type: str
    job_status: str
    session_status: str
    session_id: UUID | None
    current_job_id: UUID
    regeneration_count: int
    candidates: tuple[ReviewCandidate, ...]
    source_product_ids: tuple[str, ...]
    source_asset_ids: tuple[UUID, ...]
    publishable: Literal[False]
    approved_candidate_id: UUID | None = None
    approved_associations: tuple[ApprovedReviewAssociation, ...] = ()


@dataclass(frozen=True)
class ProductImage:
    image_kind: BridgeImageKind
    asset_id: UUID | None
    candidate_id: UUID | None
    identity: str
    content_sha256: str
    mime_type: BridgeImageMimeType
    width: int
    height: int
    customer_approved: bool | None
    content_job_id: UUID | None
    source_product_ids: tuple[str, ...]
    source_asset_ids: tuple[UUID, ...]
    publishable: Literal[False]


@dataclass(frozen=True)
class ProductImages:
    product_id: str
    requested_view: ProductView
    resolved_view: ResolvedProductView
    images: tuple[ProductImage, ...]
    publishable: Literal[False]


@dataclass(frozen=True)
class ReviewDecisionResult:
    review_id: UUID
    job_id: UUID
    candidate_id: UUID
    decision: ReviewDecision
    reason: str | None
    status: ReviewOutcomeStatus
    candidate_state: ReviewOutcomeStatus
    image_kind: BridgeImageKind
    customer_approved: bool
    source_product_ids: tuple[str, ...]
    source_asset_ids: tuple[UUID, ...]
    product_associations: tuple[tuple[str, ProductImage, ProductImage], ...]
    session_id: UUID
    next_job_id: UUID | None
    regeneration_count: int
    session_status: str
    publishable: Literal[False]
    outcome: str
    current_session_status: str
    current_job_id: UUID
    current_regeneration_count: int
    suite_state: str | None


class StyleBridgeClient:
    """Make one local-only staging and canonical-handoff attempt at a time."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        base_url: str = "http://127.0.0.1:8017",
        transport: httpx.AsyncBaseTransport | None = None,
        session_nonce: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        handoff_timeout_seconds: float = _DEFAULT_HANDOFF_TIMEOUT_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_media_bytes: int = _DEFAULT_MAX_MEDIA_BYTES,
    ) -> None:
        if timeout_seconds <= 0 or handoff_timeout_seconds <= 0:
            raise StyleBridgeConfigurationError("style_bridge_invalid_timeout")
        if max_response_bytes <= 0 or max_media_bytes <= 0:
            raise StyleBridgeConfigurationError("style_bridge_invalid_limit")
        self._enabled = enabled
        self._base_url = _validate_base_url(base_url, transport)
        if enabled and not session_nonce:
            raise StyleBridgeConfigurationError("style_bridge_nonce_required")
        self._transport = transport
        self._session_nonce = session_nonce
        self._timeout_seconds = timeout_seconds
        self._handoff_timeout_seconds = handoff_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_media_bytes = max_media_bytes

    async def health(self) -> bool:
        """Return whether the configured local consumer reports its identity."""

        response = await self._request(
            "GET",
            "/health",
            headers={"X-Local-Classification-Bridge-Nonce": self._nonce_header()},
        )
        if response.status_code != 200:
            return False
        payload = _json_object(response)
        return payload == {"status": "ok", "service": "opc-style-agent"}

    async def stage_media(self, photo: ConfirmedLocalPhoto) -> MediaReceipt:
        """Read an owned handle, verify its bytes, and stage them exactly once."""

        content = _read_owned_content(photo, self._max_media_bytes)
        response = await self._request(
            "POST",
            f"{_BRIDGE_PREFIX}/media",
            content=content,
            headers={
                "Content-Type": photo.mime_type,
                "X-Photo-Id": str(photo.photo_id),
                "X-Content-Sha256": photo.content_sha256,
                "X-Local-Classification-Bridge-Nonce": self._nonce_header(),
            },
        )
        _raise_consumer_error(response, _STAGE_MEDIA_ERRORS, envelope="error")
        if response.status_code not in {200, 201}:
            raise StyleBridgeProtocolError("style_bridge_media_stage_unexpected_status")
        return _parse_media_receipt(_json_object(response), photo)

    async def submit_handoff(self, handoff: ClassificationStyleHandoffV1) -> BridgeResult:
        """Submit exactly the shared canonical bytes; this method never retries."""

        response = await self._request(
            "POST",
            f"{_BRIDGE_PREFIX}/handoffs",
            content=canonical_handoff_bytes(handoff),
            headers={
                "Content-Type": "application/json",
                "X-Local-Classification-Bridge-Nonce": self._nonce_header(),
            },
            timeout_seconds=self._handoff_timeout_seconds,
        )
        _raise_consumer_error(response, _HANDOFF_ERRORS, envelope="error")
        if response.status_code not in {200, 201}:
            raise StyleBridgeProtocolError("style_bridge_handoff_unexpected_status")
        return _parse_bridge_result(_json_object(response), handoff.handoff_id)

    async def get_handoff(self, handoff_id: UUID) -> BridgeResult:
        """Replay the consumer's durable current status for an existing handoff."""

        response = await self._request(
            "GET",
            f"{_BRIDGE_PREFIX}/handoffs/{handoff_id}",
            headers={"X-Local-Classification-Bridge-Nonce": self._nonce_header()},
        )
        _raise_consumer_error(response, _HANDOFF_READ_ERRORS, envelope="error")
        if response.status_code == 404:
            raise StyleBridgeProtocolError("style_bridge_handoff_not_found")
        if response.status_code != 200:
            raise StyleBridgeProtocolError("style_bridge_handoff_status_unexpected_status")
        return _parse_bridge_result(_json_object(response), handoff_id)

    async def get_review_job(self, job_id: UUID) -> ReviewJob:
        """Read one nonce-protected review job without trusting a returned URL."""

        response = await self._request(
            "GET",
            f"{_BRIDGE_PREFIX}/image-outputs/{job_id}",
            headers={"X-Local-Classification-Bridge-Nonce": self._nonce_header()},
        )
        _raise_consumer_error(response, _REVIEW_JOB_ERRORS, envelope="detail")
        if response.status_code != 200:
            raise StyleBridgeProtocolError("style_bridge_review_job_unexpected_status")
        return _parse_review_job(_json_object(response), job_id)

    async def get_candidate_content(self, job_id: UUID, candidate_id: UUID) -> BridgeImage:
        """Read only one candidate byte stream scoped by its immutable job id."""

        response = await self._request(
            "GET",
            f"{_BRIDGE_PREFIX}/image-outputs/{job_id}/candidates/{candidate_id}/content",
            headers={"X-Local-Classification-Bridge-Nonce": self._nonce_header()},
            response_limit=self._max_media_bytes,
        )
        _raise_consumer_error(response, _CANDIDATE_CONTENT_ERRORS, envelope="detail")
        if response.status_code != 200:
            raise StyleBridgeProtocolError("style_bridge_candidate_content_unexpected_status")
        return _parse_bridge_image(response, self._max_media_bytes)

    async def get_real_content(self, job_id: UUID, asset_id: UUID) -> BridgeImage:
        """Read an original only when the caller supplies its owning review job."""

        response = await self._request(
            "GET",
            f"{_BRIDGE_PREFIX}/assets/{asset_id}/content?job_id={job_id}",
            headers={"X-Local-Classification-Bridge-Nonce": self._nonce_header()},
            response_limit=self._max_media_bytes,
        )
        _raise_consumer_error(response, _REAL_CONTENT_ERRORS, envelope="detail")
        if response.status_code != 200:
            raise StyleBridgeProtocolError("style_bridge_real_content_unexpected_status")
        return _parse_bridge_image(response, self._max_media_bytes)

    async def review_candidate(
        self,
        job_id: UUID,
        candidate_id: UUID,
        *,
        decision: ReviewDecision,
        reason: str | None,
        idempotency_key: str,
    ) -> ReviewDecisionResult:
        """Submit one idempotent customer decision to the candidate's own job."""

        reason = _validate_review_request(decision, reason, idempotency_key)
        response = await self._request(
            "POST",
            f"{_BRIDGE_PREFIX}/image-outputs/{job_id}/candidates/{candidate_id}/review",
            content=json.dumps(
                {
                    "decision": decision,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                },
                separators=(",", ":"),
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Local-Classification-Bridge-Nonce": self._nonce_header(),
            },
        )
        _raise_consumer_error(response, _REVIEW_CANDIDATE_ERRORS, envelope="detail")
        if response.status_code not in {200, 201}:
            raise StyleBridgeProtocolError("style_bridge_review_decision_unexpected_status")
        return _parse_review_decision(
            _json_object(response), job_id, candidate_id, decision, reason
        )

    async def get_product_images(
        self, product_id: str, handoff_id: UUID, *, view: ProductView = "default"
    ) -> ProductImages:
        """Read the requested local product view within the named handoff only."""

        if not isinstance(product_id, str) or not product_id or "/" in product_id:
            raise StyleBridgeProtocolError("style_bridge_product_id_invalid")
        if view not in _PRODUCT_VIEWS:
            raise StyleBridgeProtocolError("style_bridge_product_view_invalid")
        response = await self._request(
            "GET",
            f"{_BRIDGE_PREFIX}/products/{product_id}/images?handoff_id={handoff_id}&view={view}",
            headers={"X-Local-Classification-Bridge-Nonce": self._nonce_header()},
        )
        _raise_consumer_error(response, _PRODUCT_IMAGES_ERRORS, envelope="detail")
        if response.status_code != 200:
            raise StyleBridgeProtocolError("style_bridge_product_images_unexpected_status")
        return _parse_product_images(_json_object(response), product_id, handoff_id, view)

    async def stage_and_submit(
        self,
        photos: Sequence[ConfirmedLocalPhoto],
        build_handoff: Callable[[Mapping[UUID, MediaReceipt]], ClassificationStyleHandoffV1],
    ) -> BridgeResult:
        """Stage every confirmed byte stream before building and submitting one package.

        Failures deliberately leave any successful stage receipts recoverable at
        the consumer.  The client does not perform a second request, because it
        cannot know whether a timeout was observed before or after receipt.
        """

        receipts: dict[UUID, MediaReceipt] = {}
        for photo in photos:
            if photo.photo_id in receipts:
                raise StyleBridgeProtocolError("style_bridge_duplicate_photo")
            receipts[photo.photo_id] = await self.stage_media(photo)
        return await self.submit_handoff(build_handoff(receipts))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        response_limit: int | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        self._require_enabled()
        max_response_bytes = response_limit or self._max_response_bytes
        request_timeout = timeout_seconds or self._timeout_seconds
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(request_timeout),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with asyncio.timeout(request_timeout):
                    async with client.stream(
                        method, url, content=content, headers=headers
                    ) as source_response:
                        if source_response.is_redirect:
                            raise StyleBridgeProtocolError("style_bridge_redirect_rejected")
                        content_length = source_response.headers.get("Content-Length")
                        if content_length is not None:
                            try:
                                response_size = int(content_length)
                            except ValueError as error:
                                raise StyleBridgeProtocolError(
                                    "style_bridge_response_invalid"
                                ) from error
                            if response_size > max_response_bytes:
                                raise StyleBridgeProtocolError("style_bridge_response_too_large")
                        response_body = bytearray()
                        async for chunk in source_response.aiter_bytes():
                            remaining = max_response_bytes - len(response_body)
                            if len(chunk) > remaining:
                                raise StyleBridgeProtocolError("style_bridge_response_too_large")
                            response_body.extend(chunk)
                        response = httpx.Response(
                            source_response.status_code,
                            headers=source_response.headers,
                            content=bytes(response_body),
                            request=source_response.request,
                        )
        except TimeoutError as error:
            raise StyleBridgeTransportError("style_bridge_timeout") from error
        except httpx.RequestError as error:
            raise StyleBridgeTransportError("style_bridge_transport_unavailable") from error
        return response

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise StyleBridgeDisabledError("local_classification_bridge_disabled")

    def _nonce_header(self) -> str:
        self._require_enabled()
        if not self._session_nonce:
            raise StyleBridgeConfigurationError("style_bridge_nonce_required")
        return self._session_nonce


def _validate_base_url(base_url: str, transport: httpx.AsyncBaseTransport | None) -> str:
    """Allow a numeric loopback socket, or an explicitly in-process ASGI app."""

    parts = urlsplit(base_url)
    try:
        port = parts.port
    except ValueError as error:
        raise StyleBridgeConfigurationError("style_bridge_loopback_required") from error
    if isinstance(transport, httpx.ASGITransport):
        if base_url != _ASGI_TEST_ORIGIN:
            raise StyleBridgeConfigurationError("style_bridge_loopback_required")
        return _ASGI_TEST_ORIGIN
    if transport is not None:
        raise StyleBridgeConfigurationError("style_bridge_transport_rejected")
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise StyleBridgeConfigurationError("style_bridge_loopback_required")
    return f"http://127.0.0.1:{port}"


def _read_owned_content(photo: ConfirmedLocalPhoto, max_media_bytes: int) -> bytes:
    """Consume the supplied owned handle without transmitting its provenance."""

    try:
        handle = photo.open_content()
    except OSError as error:
        raise StyleBridgeTransportError("style_bridge_media_unavailable") from error
    if not hasattr(handle, "read"):
        raise StyleBridgeProtocolError("style_bridge_media_handle_invalid")
    try:
        content = handle.read(max_media_bytes + 1)
    finally:
        handle.close()
    if not isinstance(content, bytes):
        raise StyleBridgeProtocolError("style_bridge_media_handle_invalid")
    if not content or len(content) > max_media_bytes:
        raise StyleBridgeProtocolError("style_bridge_media_size_invalid")
    if sha256(content).hexdigest() != photo.content_sha256:
        raise StyleBridgeProtocolError("style_bridge_media_hash_mismatch")
    return content


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise StyleBridgeProtocolError("style_bridge_response_invalid") from error
    if not isinstance(payload, dict):
        raise StyleBridgeProtocolError("style_bridge_response_invalid")
    return payload


def _raise_consumer_error(
    response: httpx.Response,
    allowed_errors: Mapping[str, tuple[int, bool]],
    *,
    envelope: Literal["error", "detail"],
) -> None:
    if response.status_code < 400 or response.status_code == 404:
        return
    payload = _json_object(response)
    if envelope == "detail" and set(payload) == {"detail"} and isinstance(payload["detail"], str):
        code = payload["detail"]
    elif envelope == "error" and set(payload) == {"error"} and isinstance(payload["error"], dict):
        error = cast(dict[str, Any], payload["error"])
        if set(error) != {"code", "retryable"} or not isinstance(error["code"], str):
            raise StyleBridgeProtocolError("style_bridge_response_invalid")
        code = error["code"]
    else:
        raise StyleBridgeProtocolError("style_bridge_response_invalid")
    expected = allowed_errors.get(code)
    if expected is None:
        raise StyleBridgeProtocolError("style_bridge_response_invalid")
    expected_status, retryable = expected
    if response.status_code != expected_status:
        raise StyleBridgeProtocolError("style_bridge_response_invalid")
    if envelope == "error" and payload["error"]["retryable"] is not retryable:
        raise StyleBridgeProtocolError("style_bridge_response_invalid")
    raise StyleBridgeRemoteError(code, retryable=retryable)


def _parse_media_receipt(payload: dict[str, Any], photo: ConfirmedLocalPhoto) -> MediaReceipt:
    try:
        receipt = MediaReceipt(
            receipt_id=UUID(_require_string(payload, "receipt_id")),
            photo_id=UUID(_require_string(payload, "photo_id")),
            content_sha256=_require_string(payload, "content_sha256"),
            mime_type=_require_mime_type(payload, "mime_type"),
            width=_require_positive_int(payload, "width"),
            height=_require_positive_int(payload, "height"),
            expires_at=datetime.fromisoformat(_require_string(payload, "expires_at")),
        )
    except (TypeError, ValueError) as error:
        raise StyleBridgeProtocolError("style_bridge_media_receipt_invalid") from error
    if (
        receipt.photo_id != photo.photo_id
        or receipt.content_sha256 != photo.content_sha256
        or receipt.mime_type != photo.mime_type
        or receipt.width != photo.width
        or receipt.height != photo.height
    ):
        raise StyleBridgeProtocolError("style_bridge_media_receipt_mismatch")
    return receipt


def _parse_bridge_result(payload: dict[str, Any], expected_handoff_id: UUID) -> BridgeResult:
    try:
        handoff_id = UUID(_require_string(payload, "handoff_id"))
        status = _require_bridge_status(payload, "status")
        job_ids = payload["output_job_ids"]
        created = payload["created"]
        publishable = payload["publishable"]
        if not isinstance(job_ids, list) or type(created) is not bool or publishable is not False:
            raise TypeError("invalid bridge result")
        result = BridgeResult(
            handoff_id=handoff_id,
            status=status,
            output_job_ids=tuple(UUID(value) for value in job_ids),
            created=created,
            publishable=False,
            output_jobs=tuple(
                _parse_bridge_job_summary(item)
                for item in _require_list(payload.get("output_jobs", []), "output_jobs")
            ),
        )
        if "output_jobs" in payload and (
            tuple(item.job_id for item in result.output_jobs) != result.output_job_ids
        ):
            raise TypeError("output_jobs_mismatch")
    except (KeyError, TypeError, ValueError) as error:
        raise StyleBridgeProtocolError("style_bridge_handoff_result_invalid") from error
    if result.handoff_id != expected_handoff_id:
        raise StyleBridgeProtocolError("style_bridge_handoff_result_mismatch")
    return result


def _parse_bridge_job_summary(payload: Any) -> BridgeJobSummary:
    if not isinstance(payload, dict):
        raise TypeError("output_job")
    item = payload
    _require_exact_keys(item, {"job_id", "output_type", "status"})
    return BridgeJobSummary(
        job_id=_require_uuid(item, "job_id"),
        output_type=_require_review_output_type(item, "output_type"),
        status=_require_review_job_status(item, "status"),
    )


def _parse_bridge_image(response: httpx.Response, max_media_bytes: int) -> BridgeImage:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type not in _BRIDGE_IMAGE_MIME_TYPES:
        raise StyleBridgeProtocolError("style_bridge_media_type_invalid")
    content = response.content
    if not content or len(content) > max_media_bytes:
        raise StyleBridgeProtocolError("style_bridge_media_size_invalid")
    return BridgeImage(content=content, mime_type=cast(BridgeImageMimeType, content_type))


def _parse_review_job(payload: dict[str, Any], expected_job_id: UUID) -> ReviewJob:
    try:
        _require_exact_keys(
            payload,
            {
                "job_id",
                "handoff_id",
                "output_type",
                "job_status",
                "status",
                "session_id",
                "current_job_id",
                "regeneration_count",
                "candidates",
                "source_product_ids",
                "source_asset_ids",
                "approved_candidate_id",
                "approved_associations",
                "publishable",
            },
        )
        job_id = _require_uuid(payload, "job_id")
        handoff_id = _require_uuid(payload, "handoff_id")
        output_type = _require_review_output_type(payload, "output_type")
        job_status = _require_review_job_status(payload, "job_status")
        session_status = _require_review_session_status(payload, "status")
        session_id = _require_optional_uuid(payload, "session_id")
        current_job_id = _require_uuid(payload, "current_job_id")
        regeneration_count = _require_nonnegative_int(payload, "regeneration_count")
        candidates_payload = payload["candidates"]
        candidates = _require_list(candidates_payload, "candidates")
        source_product_ids = _require_string_list(payload, "source_product_ids")
        source_asset_ids = _require_uuid_list(payload, "source_asset_ids")
        if payload["publishable"] is not False:
            raise TypeError("publishable")
        result = ReviewJob(
            job_id=job_id,
            handoff_id=handoff_id,
            output_type=output_type,
            job_status=job_status,
            session_status=session_status,
            session_id=session_id,
            current_job_id=current_job_id,
            regeneration_count=regeneration_count,
            candidates=tuple(_parse_review_candidate(item, job_id) for item in candidates),
            source_product_ids=source_product_ids,
            source_asset_ids=source_asset_ids,
            publishable=False,
            approved_candidate_id=_require_optional_uuid(payload, "approved_candidate_id"),
            approved_associations=tuple(
                _parse_approved_review_association(item)
                for item in _require_list(payload["approved_associations"], "approved_associations")
            ),
        )
        if result.job_status == "customer_approved":
            if (
                result.approved_candidate_id
                not in {item.candidate_id for item in result.candidates}
                or not result.approved_associations
            ):
                raise TypeError("approved_candidate_missing")
        elif result.approved_candidate_id is not None or result.approved_associations:
            raise TypeError("unexpected_approval")
        if any(
            item.candidate_id != result.approved_candidate_id
            or item.product_id not in result.source_product_ids
            or item.asset_id not in result.source_asset_ids
            for item in result.approved_associations
        ):
            raise TypeError("approved_association_mismatch")
    except (KeyError, TypeError, ValueError) as error:
        raise StyleBridgeProtocolError("style_bridge_review_job_invalid") from error
    if result.job_id != expected_job_id:
        raise StyleBridgeProtocolError("style_bridge_review_job_mismatch")
    empty_manual_failure = (
        result.session_status == "manual_attention_required"
        and result.job_status in {"generation_failed", "cleanup_pending"}
        and result.session_id is not None
        and result.current_job_id == result.job_id
        and 1 <= result.regeneration_count <= 3
    )
    if not result.candidates and not empty_manual_failure:
        raise StyleBridgeProtocolError("style_bridge_review_job_invalid")
    if len({candidate.candidate_id for candidate in result.candidates}) != len(result.candidates):
        raise StyleBridgeProtocolError("style_bridge_review_job_invalid")
    return result


def _parse_approved_review_association(payload: Any) -> ApprovedReviewAssociation:
    if not isinstance(payload, dict):
        raise TypeError("approved_association")
    item = payload
    _require_exact_keys(item, {"product_id", "candidate_id", "asset_id"})
    return ApprovedReviewAssociation(
        product_id=_require_string(item, "product_id"),
        candidate_id=_require_uuid(item, "candidate_id"),
        asset_id=_require_uuid(item, "asset_id"),
    )


def _parse_review_candidate(payload: Any, expected_job_id: UUID) -> ReviewCandidate:
    if not isinstance(payload, dict):
        raise TypeError("candidate")
    _require_exact_keys(
        payload,
        {
            "candidate_id",
            "sequence_number",
            "content_sha256",
            "mime_type",
            "width",
            "height",
            "identity",
            "content_url",
            "publishable",
        },
    )
    candidate_id = _require_uuid(payload, "candidate_id")
    if payload["publishable"] is not False:
        raise TypeError("publishable")
    _require_candidate_content_url(payload, "content_url", expected_job_id, candidate_id)
    return ReviewCandidate(
        candidate_id=candidate_id,
        sequence_number=_require_positive_int(payload, "sequence_number"),
        content_sha256=_require_sha256(payload, "content_sha256"),
        mime_type=_require_bridge_image_mime_type(payload, "mime_type"),
        width=_require_positive_int(payload, "width"),
        height=_require_positive_int(payload, "height"),
        identity=_require_nonempty_string(payload, "identity"),
    )


def _parse_review_decision(
    payload: dict[str, Any],
    expected_job_id: UUID,
    expected_candidate_id: UUID,
    expected_decision: ReviewDecision,
    expected_reason: str | None,
) -> ReviewDecisionResult:
    try:
        _require_exact_keys(
            payload,
            {
                "review_id",
                "job_id",
                "candidate_id",
                "decision",
                "reason",
                "job_status",
                "candidate_state",
                "image_kind",
                "customer_approved",
                "publishable",
                "source_product_ids",
                "source_asset_ids",
                "product_associations",
                "created_at",
                "outcome",
                "current_session_status",
                "current_job_id",
                "current_regeneration_count",
                "session_id",
                "next_job_id",
                "regeneration_count",
                "status",
                "suite_state",
            },
        )
        job_id = _require_uuid(payload, "job_id")
        candidate_id = _require_uuid(payload, "candidate_id")
        decision = _require_review_decision(payload, "decision")
        reason = _require_optional_string(payload, "reason")
        status = _require_review_outcome_status(payload, "job_status")
        candidate_state = _require_review_outcome_status(payload, "candidate_state")
        image_kind = _require_bridge_image_kind(payload, "image_kind")
        customer_approved = _require_bool(payload, "customer_approved")
        if payload["publishable"] is not False:
            raise TypeError("publishable")
        datetime.fromisoformat(_require_nonempty_string(payload, "created_at"))
        associations = _require_list(payload["product_associations"], "product_associations")
        result = ReviewDecisionResult(
            review_id=_require_uuid(payload, "review_id"),
            job_id=job_id,
            candidate_id=candidate_id,
            decision=decision,
            reason=reason,
            status=status,
            candidate_state=candidate_state,
            image_kind=image_kind,
            customer_approved=customer_approved,
            source_product_ids=_require_string_list(payload, "source_product_ids"),
            source_asset_ids=_require_uuid_list(payload, "source_asset_ids"),
            product_associations=tuple(_parse_product_association(item) for item in associations),
            session_id=_require_uuid(payload, "session_id"),
            next_job_id=_require_optional_uuid(payload, "next_job_id"),
            regeneration_count=_require_nonnegative_int(payload, "regeneration_count"),
            session_status=_require_review_session_status(payload, "status"),
            publishable=False,
            outcome=_require_nonempty_string(payload, "outcome"),
            current_session_status=_require_review_session_status(
                payload, "current_session_status"
            ),
            current_job_id=_require_uuid(payload, "current_job_id"),
            current_regeneration_count=_require_nonnegative_int(
                payload, "current_regeneration_count"
            ),
            suite_state=_require_optional_string(payload, "suite_state"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StyleBridgeProtocolError("style_bridge_review_decision_invalid") from error
    if result.job_id != expected_job_id or result.candidate_id != expected_candidate_id:
        raise StyleBridgeProtocolError("style_bridge_review_decision_mismatch")
    if result.decision != expected_decision or result.reason != expected_reason:
        raise StyleBridgeProtocolError("style_bridge_review_decision_invalid")
    _validate_review_decision_state(result)
    return result


def _parse_product_images(
    payload: dict[str, Any], expected_product_id: str, handoff_id: UUID, view: ProductView
) -> ProductImages:
    try:
        _require_exact_keys(
            payload,
            {"product_id", "requested_view", "resolved_view", "images", "publishable"},
        )
        product_id = _require_nonempty_string(payload, "product_id")
        requested_view = _require_product_view(payload, "requested_view")
        resolved_view = _require_resolved_product_view(payload, "resolved_view")
        images = _require_list(payload["images"], "images")
        if payload["publishable"] is not False:
            raise TypeError("publishable")
        result = ProductImages(
            product_id=product_id,
            requested_view=requested_view,
            resolved_view=resolved_view,
            images=tuple(_parse_product_image(item, handoff_id) for item in images),
            publishable=False,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StyleBridgeProtocolError("style_bridge_product_images_invalid") from error
    if result.product_id != expected_product_id or result.requested_view != view:
        raise StyleBridgeProtocolError("style_bridge_product_images_mismatch")
    return result


def _parse_product_association(payload: Any) -> tuple[str, ProductImage, ProductImage]:
    if not isinstance(payload, dict):
        raise TypeError("product_association")
    _require_exact_keys(payload, {"product_id", "approved_image", "real_photo"})
    return (
        _require_nonempty_string(payload, "product_id"),
        _parse_product_image(payload["approved_image"], None, allow_content_url=False),
        _parse_product_image(payload["real_photo"], None, allow_content_url=False),
    )


def _parse_product_image(
    payload: Any, handoff_id: UUID | None, *, allow_content_url: bool = True
) -> ProductImage:
    if not isinstance(payload, dict):
        raise TypeError("product_image")
    image_kind = _require_bridge_image_kind(payload, "image_kind")
    if image_kind == "style_image":
        expected = {
            "image_kind",
            "candidate_id",
            "identity",
            "content_sha256",
            "mime_type",
            "width",
            "height",
            "customer_approved",
            "publishable",
            "source_product_ids",
            "source_asset_ids",
        }
        if allow_content_url:
            expected.add("content_url")
        _require_exact_keys(payload, expected)
        candidate_id = _require_uuid(payload, "candidate_id")
        if _require_bool(payload, "customer_approved") is not True:
            raise TypeError("customer_approved")
        if allow_content_url:
            content_job_id = _require_product_candidate_content_url(payload, candidate_id)
        else:
            content_job_id = None
        asset_id = None
        customer_approved: bool | None = True
    else:
        expected = {
            "image_kind",
            "asset_id",
            "identity",
            "content_sha256",
            "mime_type",
            "width",
            "height",
        }
        if allow_content_url:
            expected.add("content_url")
        _require_exact_keys(payload, expected)
        asset_id = _require_uuid(payload, "asset_id")
        if allow_content_url:
            content_job_id = _require_product_real_content_url(payload, asset_id)
        else:
            content_job_id = None
        candidate_id = None
        customer_approved = None
    if payload.get("publishable", False) is not False:
        raise TypeError("publishable")
    return ProductImage(
        image_kind=image_kind,
        asset_id=asset_id,
        candidate_id=candidate_id,
        identity=_require_nonempty_string(payload, "identity"),
        content_sha256=_require_sha256(payload, "content_sha256"),
        mime_type=_require_bridge_image_mime_type(payload, "mime_type"),
        width=_require_positive_int(payload, "width"),
        height=_require_positive_int(payload, "height"),
        customer_approved=customer_approved,
        content_job_id=content_job_id,
        source_product_ids=(
            _require_string_list(payload, "source_product_ids")
            if image_kind == "style_image"
            else ()
        ),
        source_asset_ids=(
            _require_uuid_list(payload, "source_asset_ids") if image_kind == "style_image" else ()
        ),
        publishable=False,
    )


def _validate_review_decision_state(result: ReviewDecisionResult) -> None:
    if not 0 <= result.regeneration_count <= result.current_regeneration_count <= 3:
        raise StyleBridgeProtocolError("style_bridge_review_decision_invalid")
    if result.decision == "approved":
        is_valid = (
            result.customer_approved is True
            and result.status == "customer_approved"
            and result.candidate_state == "customer_approved"
            and result.session_status == "approved"
            and result.next_job_id is None
            and bool(result.product_associations)
            and result.outcome == "approved"
            and result.current_session_status == "approved"
            and result.current_job_id == result.job_id
            and result.current_regeneration_count == result.regeneration_count
        )
    else:
        is_valid = (
            result.customer_approved is False
            and result.status == "customer_rejected"
            and result.candidate_state == "customer_rejected"
            and not result.product_associations
            and (
                (
                    result.outcome == "regenerated"
                    and result.session_status == "awaiting_review"
                    and result.next_job_id is not None
                    and result.current_job_id != result.job_id
                    and result.regeneration_count >= 1
                )
                or (
                    result.outcome == "manual_cap_reached"
                    and result.regeneration_count == 3
                    and result.session_status == "manual_attention_required"
                    and result.next_job_id is None
                    and result.current_session_status == "manual_attention_required"
                    and result.current_job_id == result.job_id
                )
                or (
                    result.outcome == "manual_generation_unavailable"
                    and result.session_status == "manual_attention_required"
                    and result.next_job_id is not None
                    and result.current_session_status == "manual_attention_required"
                    and result.current_job_id == result.next_job_id
                    and result.regeneration_count >= 1
                )
            )
        )
    if not is_valid:
        raise StyleBridgeProtocolError("style_bridge_review_decision_invalid")
    for product_id, approved_image, real_photo in result.product_associations:
        if (
            product_id not in result.source_product_ids
            or approved_image.image_kind != "style_image"
            or approved_image.candidate_id != result.candidate_id
            or approved_image.customer_approved is not True
            or real_photo.image_kind != "real_photo"
            or real_photo.asset_id not in result.source_asset_ids
        ):
            raise StyleBridgeProtocolError("style_bridge_review_decision_invalid")


def _validate_review_request(decision: str, reason: str | None, idempotency_key: str) -> str | None:
    if decision not in _REVIEW_DECISIONS:
        raise StyleBridgeProtocolError("style_bridge_review_decision_invalid")
    if reason is not None and (
        not isinstance(reason, str) or not reason.strip() or len(reason) > 255
    ):
        raise StyleBridgeProtocolError("style_bridge_review_reason_invalid")
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 255
    ):
        raise StyleBridgeProtocolError("style_bridge_idempotency_key_invalid")
    return reason.strip() if reason is not None else None


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str]) -> None:
    if set(payload) != expected:
        raise TypeError("unexpected_or_missing_fields")


def _require_uuid(payload: Mapping[str, Any], key: str) -> UUID:
    return UUID(_require_string(payload, key))


def _require_optional_uuid(payload: Mapping[str, Any], key: str) -> UUID | None:
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(key)
    return UUID(value)


def _require_nonempty_string(payload: Mapping[str, Any], key: str) -> str:
    value = _require_string(payload, key)
    if not value:
        raise TypeError(key)
    return value


def _require_optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(key)
    return value


def _require_nonnegative_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload[key]
    if type(value) is not int or value < 0:
        raise TypeError(key)
    return value


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise TypeError(key)
    return value


def _require_list(value: Any, key: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(key)
    return value


def _require_string_list(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    values = _require_list(payload[key], key)
    if any(not isinstance(value, str) or not value for value in values):
        raise TypeError(key)
    return tuple(cast(str, value) for value in values)


def _require_uuid_list(payload: Mapping[str, Any], key: str) -> tuple[UUID, ...]:
    return tuple(UUID(value) for value in _require_string_list(payload, key))


def _require_sha256(payload: Mapping[str, Any], key: str) -> str:
    value = _require_string(payload, key)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise TypeError(key)
    return value


def _require_bridge_image_mime_type(payload: Mapping[str, Any], key: str) -> BridgeImageMimeType:
    value = _require_string(payload, key)
    if value not in _BRIDGE_IMAGE_MIME_TYPES:
        raise TypeError(key)
    return cast(BridgeImageMimeType, value)


def _require_bridge_image_kind(payload: Mapping[str, Any], key: str) -> BridgeImageKind:
    value = _require_string(payload, key)
    if value not in _BRIDGE_IMAGE_KINDS:
        raise TypeError(key)
    return cast(BridgeImageKind, value)


def _require_review_decision(payload: Mapping[str, Any], key: str) -> ReviewDecision:
    value = _require_string(payload, key)
    if value not in _REVIEW_DECISIONS:
        raise TypeError(key)
    return cast(ReviewDecision, value)


def _require_review_outcome_status(payload: Mapping[str, Any], key: str) -> ReviewOutcomeStatus:
    value = _require_string(payload, key)
    if value not in _REVIEW_OUTCOME_STATUSES:
        raise TypeError(key)
    return cast(ReviewOutcomeStatus, value)


def _require_review_session_status(payload: Mapping[str, Any], key: str) -> str:
    value = _require_string(payload, key)
    if value not in _REVIEW_SESSION_STATUSES:
        raise TypeError(key)
    return value


def _require_review_output_type(payload: Mapping[str, Any], key: str) -> str:
    value = _require_string(payload, key)
    if value not in _REVIEW_OUTPUT_TYPES:
        raise TypeError(key)
    return value


def _require_review_job_status(payload: Mapping[str, Any], key: str) -> str:
    value = _require_string(payload, key)
    if value not in _REVIEW_JOB_STATUSES:
        raise TypeError(key)
    return value


def _require_product_view(payload: Mapping[str, Any], key: str) -> ProductView:
    value = _require_string(payload, key)
    if value not in _PRODUCT_VIEWS:
        raise TypeError(key)
    return cast(ProductView, value)


def _require_resolved_product_view(payload: Mapping[str, Any], key: str) -> ResolvedProductView:
    value = _require_string(payload, key)
    if value not in _RESOLVED_PRODUCT_VIEWS:
        raise TypeError(key)
    return cast(ResolvedProductView, value)


def _require_candidate_content_url(
    payload: Mapping[str, Any], key: str, job_id: UUID, candidate_id: UUID
) -> None:
    expected_path = f"{_BRIDGE_PREFIX}/image-outputs/{job_id}/candidates/{candidate_id}/content"
    _require_relative_url(_require_string(payload, key), expected_path, set())


def _require_product_candidate_content_url(payload: Mapping[str, Any], candidate_id: UUID) -> UUID:
    value = _require_string(payload, "content_url")
    parts = urlsplit(value)
    segments = parts.path.split("/")
    if (
        parts.scheme
        or parts.netloc
        or parts.query
        or parts.fragment
        or len(segments) != 9
        or segments[:5] != ["", "v1", "local-test", "classification-bridge", "image-outputs"]
        or segments[6] != "candidates"
        or segments[8] != "content"
    ):
        raise TypeError("content_url")
    try:
        job_id = UUID(segments[5])
    except ValueError as error:
        raise TypeError("content_url") from error
    _require_candidate_content_url({"content_url": value}, "content_url", job_id, candidate_id)
    return job_id


def _require_product_real_content_url(payload: Mapping[str, Any], asset_id: UUID) -> UUID:
    expected_path = f"{_BRIDGE_PREFIX}/assets/{asset_id}/content"
    _require_relative_url(_require_string(payload, "content_url"), expected_path, {"job_id"})
    return UUID(parse_qs(urlsplit(_require_string(payload, "content_url")).query)["job_id"][0])


def _require_relative_url(value: str, expected_path: str, expected_query_keys: set[str]) -> None:
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path != expected_path or parts.fragment:
        raise TypeError("content_url")
    query = parse_qs(parts.query, keep_blank_values=True)
    if set(query) != expected_query_keys or any(
        len(values) != 1 or not values[0] for values in query.values()
    ):
        raise TypeError("content_url")
    if expected_query_keys == {"job_id"}:
        try:
            UUID(query["job_id"][0])
        except ValueError as error:
            raise TypeError("content_url") from error


def _require_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise TypeError(key)
    return value


def _require_positive_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload[key]
    if type(value) is not int or value <= 0:
        raise TypeError(key)
    return value


def _require_bridge_status(payload: Mapping[str, Any], key: str) -> BridgeStatus:
    value = _require_string(payload, key)
    if value not in _BRIDGE_STATUSES:
        raise StyleBridgeProtocolError("style_bridge_handoff_status_invalid")
    return cast(BridgeStatus, value)


def _require_mime_type(
    payload: Mapping[str, Any], key: str
) -> Literal["image/jpeg", "image/png", "image/heic"]:
    value = _require_string(payload, key)
    if value not in {"image/jpeg", "image/png", "image/heic"}:
        raise TypeError(key)
    return cast(Literal["image/jpeg", "image/png", "image/heic"], value)
