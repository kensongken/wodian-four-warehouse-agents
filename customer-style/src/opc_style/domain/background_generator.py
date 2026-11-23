"""Provider-neutral contracts for generating an empty styled background only."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from opc_style.domain.style_reference_pack import (
    StyleReference,
    reference_manifest_sha256,
)

_FORBIDDEN_RULE_KEY = re.compile(
    r"(?:product|foreground|sha|digest|asset|path|secret|token|customer|sample.*id)",
    re.IGNORECASE,
)
_HEX_DIGEST = re.compile(r"\b[a-f0-9]{64}\b", re.IGNORECASE)
_LOCAL_PATH = re.compile(r"(?:^|\s)(?:/|\./|\.\./|[A-Za-z]:\\)")


class BackgroundGenerationError(Exception):
    """Stable failure codes for the one-attempt background boundary."""

    def __init__(self, code: str, *, outbound_attempts: int = 0) -> None:
        self.code = code
        self.outbound_attempts = outbound_attempts
        super().__init__(code)


@dataclass(frozen=True)
class BackgroundStyleReference:
    """One authorized historical style image and its sealed bytes."""

    sample_id: UUID
    content: bytes
    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png"]


@dataclass(frozen=True)
class BackgroundGenerationRequest:
    """A request that intentionally has no product or foreground field."""

    style_reference_pack_id: UUID
    style_reference_manifest_sha256: str
    references: tuple[BackgroundStyleReference, ...]
    slot_rules: dict[str, object]
    style_rules: dict[str, object]
    output_size: tuple[int, int] = (1024, 1024)


@dataclass(frozen=True)
class BackgroundGenerationPreview:
    """Exact human-authorized scope before the sole paid attempt."""

    customer_id: UUID
    style_suite_run_id: UUID
    foreground_version_id: UUID
    style_reference_pack_id: UUID
    authorized_manifest_sha256: str
    authorized_manifest: dict[str, object]
    model: str
    maximum_usd: Decimal
    provider_image_count: int
    publishable: Literal[False] = False


@dataclass(frozen=True)
class GeneratedBackground:
    """One returned empty-scene image; it still requires human review."""

    content: bytes
    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png"]
    width: int
    height: int
    metadata: dict[str, object] = field(default_factory=dict)
    publishable: Literal[False] = False


class BackgroundGenerator(Protocol):
    adapter_version: str
    model: str
    publishable: Literal[False]

    def generate(self, request: BackgroundGenerationRequest) -> GeneratedBackground:
        """Make exactly one provider attempt and return one unreviewed background."""
        ...


def validate_background_generation_request(
    request: BackgroundGenerationRequest,
) -> None:
    """Validate exact references and keep private product facts out of prompts."""
    if not 2 <= len(request.references) <= 4:
        raise BackgroundGenerationError("style_reference_count_invalid")
    if len(request.style_reference_manifest_sha256) != 64:
        raise BackgroundGenerationError("style_reference_manifest_mismatch")
    for reference in request.references:
        if (
            not reference.content
            or reference.mime_type not in {"image/jpeg", "image/png"}
            or sha256(reference.content).hexdigest() != reference.content_sha256
        ):
            raise BackgroundGenerationError("style_reference_manifest_mismatch")
    sealed = tuple(
        StyleReference(
            sample_id=reference.sample_id,
            content_sha256=reference.content_sha256,
            mime_type=reference.mime_type,
        )
        for reference in request.references
    )
    if reference_manifest_sha256(sealed) != request.style_reference_manifest_sha256:
        raise BackgroundGenerationError("style_reference_manifest_mismatch")
    if request.output_size[0] <= 0 or request.output_size[1] <= 0:
        raise BackgroundGenerationError("background_output_size_invalid")
    _validate_rules(request.slot_rules)
    _validate_rules(request.style_rules)


def _validate_rules(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _FORBIDDEN_RULE_KEY.search(key):
                raise BackgroundGenerationError("background_rule_scope_invalid")
            _validate_rules(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_rules(item)
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str) and not (
        _HEX_DIGEST.search(value) or _LOCAL_PATH.search(value)
    ):
        return
    raise BackgroundGenerationError("background_rule_scope_invalid")
