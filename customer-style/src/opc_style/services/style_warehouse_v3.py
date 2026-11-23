"""Decision and request-signing rules for independent style intake."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime

from opc_style.contracts.style_warehouse_v3 import (
    StyleWarehouseDecisionReceiptV3,
    StyleWarehouseDeliveryV3,
)
from opc_style.repositories_style_warehouse import StyleWarehouseV3Repository


class InvalidStyleWarehouseSignature(ValueError):
    pass


def style_request_signature(
    *,
    secret: str,
    timestamp: int,
    source_system: str,
    tenant_id: str,
    method: str,
    path: str,
    body: bytes,
) -> str:
    body_sha256 = hashlib.sha256(body).hexdigest()
    message = (
        f"{timestamp}\n{source_system}\n{tenant_id}\n"
        f"{method.upper()}\n{path}\n{body_sha256}"
    ).encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_style_request_signature(
    *,
    secret: str,
    signature: str,
    timestamp: str,
    source_system: str,
    tenant_id: str,
    method: str,
    path: str,
    body: bytes,
    now: datetime,
    tolerance_seconds: int,
    expected_source_system: str = "classification",
) -> None:
    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError) as error:
        raise InvalidStyleWarehouseSignature from error
    if source_system != expected_source_system:
        raise InvalidStyleWarehouseSignature
    if abs(int(now.timestamp()) - timestamp_value) > tolerance_seconds:
        raise InvalidStyleWarehouseSignature
    expected = style_request_signature(
        secret=secret,
        timestamp=timestamp_value,
        source_system=source_system,
        tenant_id=tenant_id,
        method=method,
        path=path,
        body=body,
    )
    if not hmac.compare_digest(signature, expected):
        raise InvalidStyleWarehouseSignature


class StyleWarehouseV3Service:
    def __init__(
        self,
        repository: StyleWarehouseV3Repository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repository = repository
        self.now = now

    def receive(
        self, delivery: StyleWarehouseDeliveryV3
    ) -> StyleWarehouseDecisionReceiptV3:
        payload = delivery.payload
        rejection_code: str | None = None
        if (
            payload.transform_authorization_status == "none"
            or payload.transform_authorization_id is None
        ):
            rejection_code = "style_transform_authorization_required"
        elif payload.transform_authorization_status == "revoked":
            rejection_code = "style_transform_authorization_revoked"
        elif (
            payload.transform_authorization_status == "expired"
            or payload.transform_authorization_expires_at is None
            or payload.transform_authorization_expires_at <= self.now()
        ):
            rejection_code = "style_transform_authorization_expired"
        elif (
            payload.transform_authorization_image_set_sha256
            != payload.source_image_set_sha256
        ):
            rejection_code = "style_image_set_mismatch"

        return self.repository.accept_or_replay(
            delivery,
            decision="rejected" if rejection_code else "accepted",
            rejection_code=rejection_code,
        )
