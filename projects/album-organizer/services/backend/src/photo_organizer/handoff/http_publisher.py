from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlparse

import httpx

from photo_organizer.handoff.product_image_set import (
    ProductImageSetReadyV1,
    canonical_body,
)

_ENDPOINT_PATH = "/v1/integrations/album/product-image-sets"
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429})
RETRY_DELAYS_SECONDS = (30, 120, 600, 1800, 7200)


class PublicationDisposition(StrEnum):
    DELIVERED = "delivered"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True)
class PublicationReceipt:
    disposition: PublicationDisposition
    status_code: int | None
    error_code: str | None
    retry_after_seconds: int | None = None


class ProductImageSetHttpPublisher:
    """Signed, loopback-only delivery for frozen ProductImageSetReadyV1 events."""

    def __init__(
        self,
        endpoint_url: str,
        signing_secret: bytes,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlparse(endpoint_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.path != _ENDPOINT_PATH
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("local_product_image_set_endpoint_required")
        if not signing_secret:
            raise ValueError("product_image_set_signing_secret_required")
        self._endpoint_url = endpoint_url
        self._signing_secret = signing_secret
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    async def publish(
        self,
        event: ProductImageSetReadyV1,
        *,
        attempt: int = 1,
    ) -> PublicationReceipt:
        if attempt < 1:
            raise ValueError("publication_attempt_must_be_positive")
        body = canonical_body(event)
        timestamp = int(self._clock().timestamp())
        headers = self._headers(event, body, timestamp)
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self._endpoint_url, content=body, headers=headers
                )
        except httpx.TransportError:
            return PublicationReceipt(
                PublicationDisposition.RETRYABLE_FAILURE,
                None,
                "network_error",
                self._retry_delay(attempt),
            )
        return self._classify_response(response.status_code, attempt)

    def _headers(
        self,
        event: ProductImageSetReadyV1,
        body: bytes,
        timestamp: int,
    ) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Source-System": "album",
            "X-Tenant-Id": str(event.tenant_id),
            "X-Signature-Timestamp": str(timestamp),
            "X-Signature": self._signature(event, body, timestamp),
        }

    def _signature(
        self,
        event: ProductImageSetReadyV1,
        body: bytes,
        timestamp: int,
    ) -> str:
        body_digest = hashlib.sha256(body).hexdigest()
        message = (
            f"{timestamp}\nalbum\n{event.tenant_id}\nPOST\n"
            f"{_ENDPOINT_PATH}\n{body_digest}"
        ).encode()
        return hmac.new(self._signing_secret, message, hashlib.sha256).hexdigest()

    @staticmethod
    def _classify_response(status_code: int, attempt: int) -> PublicationReceipt:
        if status_code in {200, 202}:
            return PublicationReceipt(
                PublicationDisposition.DELIVERED, status_code, None
            )
        if 300 <= status_code < 400:
            return PublicationReceipt(
                PublicationDisposition.PERMANENT_FAILURE,
                status_code,
                "redirect_refused",
            )
        if status_code in _RETRYABLE_STATUS_CODES or status_code >= 500:
            return PublicationReceipt(
                PublicationDisposition.RETRYABLE_FAILURE,
                status_code,
                f"http_{status_code}",
                ProductImageSetHttpPublisher._retry_delay(attempt),
            )
        return PublicationReceipt(
            PublicationDisposition.PERMANENT_FAILURE,
            status_code,
            f"http_{status_code}",
        )

    @staticmethod
    def _retry_delay(attempt: int) -> int:
        return RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)]
