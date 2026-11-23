"""Canonical signing for one bounded real style-suite batch preview."""

import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256

from opc_style.domain.errors import DomainRuleError


@dataclass(frozen=True)
class SignedBatchScope:
    payload: dict[str, object]
    token: str

    @classmethod
    def issue(cls, secret: bytes, payload: dict[str, object]) -> "SignedBatchScope":
        canonical = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        signature = hmac.new(secret, canonical, sha256).hexdigest()
        encoded = urlsafe_b64encode(canonical).decode("ascii").rstrip("=")
        return cls(payload=payload, token=f"{encoded}.{signature}")

    @classmethod
    def verify(cls, secret: bytes, token: str) -> "SignedBatchScope":
        try:
            encoded, signature = token.split(".", 1)
            canonical = urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            expected = hmac.new(secret, canonical, sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(canonical)
            if not isinstance(payload, dict):
                raise TypeError
        except (
            UnicodeDecodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as error:
            raise DomainRuleError("style_suite_preview_invalid") from error
        return cls(payload=payload, token=token)
