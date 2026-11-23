"""Read and verify source-image bytes from the classification warehouse."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from opc_style.contracts.style_warehouse_v3 import StyleEligibilityInputV4

ALLOWED_MIME_TYPES = {"image/heic", "image/jpeg", "image/png"}


class ClassificationMediaUnavailable(RuntimeError):
    pass


def source_image_read_authorized(
    payload: StyleEligibilityInputV4, *, now: datetime | None = None
) -> bool:
    """Do not fetch bytes unless the exact local-only authorization is active."""
    current = now or datetime.now(UTC)
    return (
        payload.transform_authorization_status == "active"
        and payload.transform_authorization_id is not None
        and payload.transform_authorization_scope == "local_style_acceptance_only"
        and payload.transform_authorization_expires_at is not None
        and payload.transform_authorization_expires_at > current
        and payload.transform_authorization_image_set_sha256
        == payload.source_image_set_sha256
        and payload.transform_authorization_revoked_at is None
    )


async def verify_source_images(
    payload: StyleEligibilityInputV4,
    *,
    base_url: str,
    secret: str,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    """Fetch every declared image over a signed, tenant-scoped private route."""
    verified: dict[str, str] = {}
    async with httpx.AsyncClient(
        transport=transport,
        timeout=15,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for image in payload.images:
            path = (
                f"/v0.1/products/{quote(payload.product_code, safe='')}/packages/"
                f"{payload.source_material_package_version}/images/{image.asset_id}/content"
            )
            timestamp = str(int(time.time()))
            body_digest = hashlib.sha256(b"").hexdigest()
            message = (
                f"{timestamp}\nstyle\n{payload.tenant_id}\nGET\n{path}\n{body_digest}"
            ).encode()
            signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
            try:
                async with client.stream(
                    "GET",
                    f"{base_url.rstrip('/')}{path}",
                    headers={
                        "X-Source-System": "style",
                        "X-Tenant-Id": str(payload.tenant_id),
                        "X-Signature-Timestamp": timestamp,
                        "X-Signature": signature,
                    },
                ) as response:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ClassificationMediaUnavailable(
                                "classification_media_integrity_failed"
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)
            except httpx.HTTPError as error:
                raise ClassificationMediaUnavailable(
                    "classification_media_unavailable"
                ) from error
            digest = hashlib.sha256(content).hexdigest()
            if (
                response.status_code != 200
                or len(content) == 0
                or len(content) > max_bytes
                or response.headers.get("content-type", "").split(";", 1)[0]
                != image.mime_type
                or image.mime_type not in ALLOWED_MIME_TYPES
                or response.headers.get("x-content-sha256") != image.sha256
                or digest != image.sha256
            ):
                raise ClassificationMediaUnavailable(
                    "classification_media_integrity_failed"
                )
            verified[str(image.asset_id)] = digest
    return verified
