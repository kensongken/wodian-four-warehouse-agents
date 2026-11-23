"""OpenAI adapter that receives style references but never product content."""

from __future__ import annotations

import base64
import json
import logging
from hashlib import sha256
from io import BytesIO
from typing import Literal

import httpx
from PIL import Image, UnidentifiedImageError

from opc_style.domain.background_generator import (
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    GeneratedBackground,
    validate_background_generation_request,
)
from opc_style.settings import RealBackgroundProviderSettings

logger = logging.getLogger(__name__)

OPENAI_BACKGROUND_ADAPTER_VERSION = "background_only_v3"
OPENAI_IMAGE_EDIT_URL = "https://api.openai.com/v1/images/edits"
OPENAI_429_CODES = {
    "credit_balance_exhausted": "background_provider_credit_exhausted",
    "organization_spend_limit_exceeded": (
        "background_provider_organization_spend_limit_exceeded"
    ),
    "project_spend_limit_exceeded": "background_provider_project_spend_limit_exceeded",
    "organization_usage_limit_exceeded": (
        "background_provider_organization_usage_limit_exceeded"
    ),
    "slow_down": "background_provider_slow_down",
}
EMPTY_SCENE_PROMPT = (
    "Generate only an empty commercial photography background matching the authorized "
    "reference lighting, palette, surface, and composition. The scene must contain no "
    "jewelry, no product, no people, no hands, no body parts, no text, no logos, and no "
    "brands. Decorative props may appear only around the perimeter. Leave the central "
    "product placement area empty and unobstructed. Treat the authorized style name as "
    "the primary art direction: its warmth and named dominant color override secondary "
    "colors seen in references. Build a believable horizontal product surface with soft "
    "light falloff and open negative space for a locally composited product. The usable "
    "horizontal tabletop MUST occupy roughly the lower 35 to 45 percent of the image, "
    "with a clear continuous surface across the central 70 percent of the frame. Keep "
    "large props, curtains, and platforms outside that central placement zone. Do not "
    "draw a product-shaped shadow or silhouette."
)


class OpenAIBackgroundGenerator:
    """One-shot paid adapter with an intentionally product-blind input type."""

    adapter_version = OPENAI_BACKGROUND_ADAPTER_VERSION
    publishable = False

    def __init__(
        self, settings: RealBackgroundProviderSettings, *, transport: httpx.Client
    ) -> None:
        if not settings.enabled or settings.api_key is None:
            raise BackgroundGenerationError("background_provider_disabled")
        self._settings = settings
        self._api_key = settings.api_key.get_secret_value()
        self._transport = transport
        self.model = settings.model

    def generate(self, request: BackgroundGenerationRequest) -> GeneratedBackground:
        validate_background_generation_request(request)
        try:
            response = self._transport.post(
                OPENAI_IMAGE_EDIT_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                data={
                    "model": self.model,
                    "prompt": self._prompt(request),
                    "n": "1",
                    "size": f"{request.output_size[0]}x{request.output_size[1]}",
                    "quality": "high",
                    "output_format": "png",
                },
                files=[
                    (
                        "image[]",
                        (
                            f"style-{index}.{self._extension(reference.mime_type)}",
                            reference.content,
                            reference.mime_type,
                        ),
                    )
                    for index, reference in enumerate(request.references, start=1)
                ],
                timeout=self._settings.timeout_seconds,
            )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
        ) as error:
            raise BackgroundGenerationError(
                "background_dispatch_uncertain", outbound_attempts=1
            ) from error
        if response.status_code in {401, 403}:
            self._log_provider_failure(response)
            raise BackgroundGenerationError(
                "background_provider_auth_failed", outbound_attempts=1
            )
        if response.status_code == 429:
            self._log_provider_failure(response)
            raise BackgroundGenerationError(
                self._failure_code_for_429(response), outbound_attempts=1
            )
        if response.status_code >= 500:
            self._log_provider_failure(response)
            raise BackgroundGenerationError(
                "background_provider_failed", outbound_attempts=1
            )
        if response.status_code >= 400:
            self._log_provider_failure(response)
            raise BackgroundGenerationError(
                "background_provider_rejected", outbound_attempts=1
            )
        return self._decode(response)

    @staticmethod
    def _log_provider_failure(response: httpx.Response) -> None:
        """Record safe diagnostics without logging credentials or response bodies."""
        provider_code = OpenAIBackgroundGenerator._provider_error_code(response)
        logger.warning(
            "OpenAI background request failed: status=%s request_id=%s "
            "provider_code=%s retry_after=%s",
            response.status_code,
            response.headers.get("x-request-id", "unavailable"),
            provider_code or "unavailable",
            response.headers.get("retry-after", "unavailable"),
        )

    @staticmethod
    def _failure_code_for_429(response: httpx.Response) -> str:
        provider_code = OpenAIBackgroundGenerator._provider_error_code(response)
        return OPENAI_429_CODES.get(
            provider_code or "", "background_provider_rate_limited"
        )

    @staticmethod
    def _provider_error_code(response: httpx.Response) -> str | None:
        """Extract only the provider's machine code, never its free-text message."""
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("code")
        return code if isinstance(code, str) and code else None

    @staticmethod
    def _prompt(request: BackgroundGenerationRequest) -> str:
        controlled = json.dumps(
            {"slot_rules": request.slot_rules, "style_rules": request.style_rules},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        named_palette = OpenAIBackgroundGenerator._named_palette_instruction(
            request.style_rules.get("name")
        )
        return (
            f"{EMPTY_SCENE_PROMPT}{named_palette}"
            f"\nAuthorized style rules: {controlled}"
        )

    @staticmethod
    def _named_palette_instruction(style_name: object) -> str:
        """Translate a confirmed Chinese art-direction name into a hard palette cue."""
        if not isinstance(style_name, str):
            return ""
        if "暖红" in style_name:
            return (
                " The dominant background color MUST be warm red, muted burgundy, "
                "terracotta, or soft rose. Blue and navy MUST NOT be dominant; use "
                "reference images only for lighting and spatial composition when their "
                "colors conflict with this requirement."
            )
        return ""

    @staticmethod
    def _extension(mime_type: str) -> str:
        return "jpg" if mime_type == "image/jpeg" else "png"

    def _decode(self, response: httpx.Response) -> GeneratedBackground:
        try:
            payload = response.json()
            encoded = payload["data"][0]["b64_json"]
            if not isinstance(encoded, str):
                raise TypeError
            content = base64.b64decode(encoded, validate=True)
            image = Image.open(BytesIO(content))
            image.load()
            if (
                image.format not in {"JPEG", "PNG"}
                or image.width <= 0
                or image.height <= 0
            ):
                raise ValueError
            mime_type: Literal["image/jpeg", "image/png"] = (
                "image/png" if image.format == "PNG" else "image/jpeg"
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            UnidentifiedImageError,
            OSError,
        ) as error:
            raise BackgroundGenerationError(
                "background_image_invalid", outbound_attempts=1
            ) from error
        return GeneratedBackground(
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type=mime_type,
            width=image.width,
            height=image.height,
            metadata={
                "adapter_version": self.adapter_version,
                "model": self.model,
                "background_only": True,
                "publishable": False,
            },
        )
