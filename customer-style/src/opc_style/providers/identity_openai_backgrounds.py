"""Paid-provider adapter that can request empty backgrounds only."""

import base64
import json
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from time import monotonic
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

from opc_style.domain.identity_background_generator import (
    BACKGROUND_COUNT,
    BACKGROUND_HEIGHT,
    BACKGROUND_RATIO,
    BACKGROUND_WIDTH,
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    GeneratedBackground,
    controlled_background_prompt,
)
from opc_style.domain.image_generator import ImageGenerationError
from opc_style.providers.openai_images import RealImageBudget
from opc_style.settings import RealImageProviderSettings

OPENAI_BACKGROUND_URL = "https://api.openai.com/v1/images/generations"
OPENAI_BACKGROUND_ADAPTER_VERSION = "openai-empty-background-v1"
PROVIDER_WIDTH = 1024
PROVIDER_HEIGHT = 1280


class OpenAIBackgroundGenerator:
    """Generate three empty scenes without sending any media to the provider."""

    adapter_version = OPENAI_BACKGROUND_ADAPTER_VERSION
    synthetic_test_only = False
    publishable = False
    requires_source_content = False

    def __init__(
        self,
        settings: RealImageProviderSettings,
        *,
        transport: httpx.Client,
        budget: RealImageBudget,
        estimated_request_usd: Decimal = Decimal("0.30"),
    ) -> None:
        if not settings.enabled or settings.api_key is None:
            raise BackgroundGenerationError("background_provider_disabled")
        if budget.limit_usd != settings.max_run_usd:
            raise BackgroundGenerationError("background_budget_snapshot_mismatch")
        self._settings = settings
        self._api_key = settings.api_key.get_secret_value()
        self._transport = transport
        self._budget = budget
        self._estimated_request_usd = estimated_request_usd

    def generate(
        self, request: BackgroundGenerationRequest
    ) -> list[GeneratedBackground]:
        _validate_request(request)
        fingerprint = _request_fingerprint(request)
        try:
            reservation = self._budget.reserve(
                fingerprint, self._estimated_request_usd
            )
        except ImageGenerationError as error:
            translations = {
                "real_image_budget_replay_blocked": "background_budget_replay_blocked",
                "real_image_budget_exhausted": "background_budget_exhausted",
            }
            raise BackgroundGenerationError(
                translations.get(error.code, "background_budget_invalid")
            ) from error

        started = monotonic()
        try:
            response = self._transport.post(
                OPENAI_BACKGROUND_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._settings.model,
                    "prompt": controlled_background_prompt(request),
                    "n": BACKGROUND_COUNT,
                    "size": f"{PROVIDER_WIDTH}x{PROVIDER_HEIGHT}",
                    "quality": "high",
                    "output_format": "jpeg",
                },
                timeout=self._settings.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as error:
            raise BackgroundGenerationError(
                "background_generation_uncertain", outbound_attempts=1
            ) from error

        _raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as error:
            raise BackgroundGenerationError(
                "background_provider_response_invalid", outbound_attempts=1
            ) from error
        backgrounds = _decode_backgrounds(
            payload,
            request_fingerprint=fingerprint,
            model=self._settings.model,
            elapsed_ms=max(0, round((monotonic() - started) * 1000)),
        )
        try:
            self._budget.commit(reservation, self._estimated_request_usd)
        except ImageGenerationError as error:
            raise BackgroundGenerationError(
                "background_budget_exhausted", outbound_attempts=1
            ) from error
        return backgrounds


def _validate_request(request: BackgroundGenerationRequest) -> None:
    if request.output_ratio != BACKGROUND_RATIO:
        raise BackgroundGenerationError("background_ratio_unsupported")
    if request.candidate_count != BACKGROUND_COUNT:
        raise BackgroundGenerationError("background_candidate_count_invalid")


def _request_fingerprint(request: BackgroundGenerationRequest) -> str:
    canonical = json.dumps(
        {
            "style_observations": request.style_observations,
            "slot_rules": request.slot_rules,
            "output_ratio": request.output_ratio,
            "candidate_count": request.candidate_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code in {401, 403}:
        code = "background_provider_auth_failed"
    elif response.status_code == 429:
        code = "background_provider_rate_limited"
    elif response.status_code >= 500:
        code = "background_generation_uncertain"
    elif response.status_code >= 400:
        code = "background_provider_request_rejected"
    else:
        return
    raise BackgroundGenerationError(code, outbound_attempts=1)


def _decode_backgrounds(
    payload: Any,
    *,
    request_fingerprint: str,
    model: str,
    elapsed_ms: int,
) -> list[GeneratedBackground]:
    if not isinstance(payload, dict):
        raise BackgroundGenerationError(
            "background_provider_response_invalid", outbound_attempts=1
        )
    raw_items = payload.get("data")
    if not isinstance(raw_items, list) or len(raw_items) != BACKGROUND_COUNT:
        raise BackgroundGenerationError(
            "background_generator_must_return_exactly_three", outbound_attempts=1
        )
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    result: list[GeneratedBackground] = []
    for sequence_number, item in enumerate(raw_items, start=1):
        content = _decode_and_crop(item)
        result.append(
            GeneratedBackground(
                content=content,
                content_sha256=sha256(content).hexdigest(),
                mime_type="image/jpeg",
                width=BACKGROUND_WIDTH,
                height=BACKGROUND_HEIGHT,
                sequence_number=sequence_number,
                metadata={
                    "adapter_version": OPENAI_BACKGROUND_ADAPTER_VERSION,
                    "provider": "openai",
                    "model": model,
                    "provider_size": f"{PROVIDER_WIDTH}x{PROVIDER_HEIGHT}",
                    "final_size": f"{BACKGROUND_WIDTH}x{BACKGROUND_HEIGHT}",
                    "request_fingerprint": request_fingerprint,
                    "elapsed_ms": elapsed_ms,
                    "outbound_attempts": 1,
                    "usage": usage,
                },
                synthetic_test_only=False,
                publishable=False,
            )
        )
    return result


def _decode_and_crop(item: object) -> bytes:
    try:
        encoded = item["b64_json"] if isinstance(item, dict) else None
        if not isinstance(encoded, str):
            raise TypeError
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(raw)) as image:
            image.load()
            if image.format != "JPEG" or image.size != (PROVIDER_WIDTH, PROVIDER_HEIGHT):
                raise ValueError
            cropped = image.convert("RGB")
        output = BytesIO()
        cropped.save(output, format="JPEG", quality=95, optimize=False, progressive=False)
        return output.getvalue()
    except (KeyError, TypeError, ValueError, OSError, UnidentifiedImageError) as error:
        raise BackgroundGenerationError(
            "background_provider_response_invalid", outbound_attempts=1
        ) from error
