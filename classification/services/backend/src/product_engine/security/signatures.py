import hashlib
import hmac
from uuid import UUID

from product_engine.domain.errors import InvalidSignature


def _message(
    timestamp: int,
    source_system: str,
    tenant_id: UUID,
    method: str,
    path: str,
    body: bytes,
) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"{timestamp}\n{source_system}\n{tenant_id}\n{method.upper()}\n{path}\n{digest}"
    ).encode()


def sign_request(
    secret: bytes,
    timestamp: int,
    source_system: str,
    tenant_id: UUID,
    method: str,
    path: str,
    body: bytes,
) -> str:
    message = _message(timestamp, source_system, tenant_id, method, path, body)
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_request_signature(
    *,
    secret: bytes,
    timestamp: int,
    now: int,
    tolerance_seconds: int,
    source_system: str,
    tenant_id: UUID,
    method: str,
    path: str,
    body: bytes,
    supplied_signature: str,
) -> None:
    if abs(now - timestamp) > tolerance_seconds:
        raise InvalidSignature("signature_timestamp_out_of_range")
    expected = sign_request(
        secret,
        timestamp,
        source_system,
        tenant_id,
        method,
        path,
        body,
    )
    if not hmac.compare_digest(expected, supplied_signature):
        raise InvalidSignature("signature_mismatch")
