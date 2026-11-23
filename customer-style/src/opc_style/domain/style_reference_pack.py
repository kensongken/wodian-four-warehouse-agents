"""Immutable manifests for the real style images sent to a provider."""

import base64
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal
from uuid import UUID


def style_reference_access_token(
    secret: bytes, customer_id: UUID, pack_id: UUID, sample_id: UUID
) -> str:
    """Sign one exact local reference-thumbnail lineage."""
    payload = customer_id.bytes
    signature = hmac.new(
        secret, b"style-reference" + payload + pack_id.bytes + sample_id.bytes, sha256
    ).digest()
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def resolve_style_reference_customer(
    secret: bytes, pack_id: UUID, sample_id: UUID, token: str
) -> UUID | None:
    """Resolve the customer only when the exact thumbnail lineage is signed."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        customer_id = UUID(bytes=raw[:16])
    except (ValueError, TypeError):
        return None
    expected = style_reference_access_token(secret, customer_id, pack_id, sample_id)
    return customer_id if hmac.compare_digest(token, expected) else None


@dataclass(frozen=True)
class StyleReference:
    """One sealed customer-owned style image in provider order."""

    sample_id: UUID
    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png"]


@dataclass(frozen=True)
class StyleReferencePackSnapshot:
    """One stable style-reference selection for a confirmed style version."""

    id: UUID
    customer_id: UUID
    scene_style_version_id: UUID
    selection_version: str
    manifest_sha256: str
    confirmed_by: str
    active: bool
    created_at: datetime
    references: tuple[StyleReference, ...]


def reference_manifest_sha256(
    references: tuple[StyleReference, ...],
) -> str:
    """Hash the exact ordered image identities and bytes."""

    payload = [
        {
            "sample_id": str(reference.sample_id),
            "content_sha256": reference.content_sha256,
            "mime_type": reference.mime_type,
        }
        for reference in references
    ]
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(canonical).hexdigest()
