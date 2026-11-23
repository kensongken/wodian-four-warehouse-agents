"""No-network tests for the paid background-only provider adapter."""

import base64
import json
from decimal import Decimal
from io import BytesIO

import httpx
import pytest
from PIL import Image

from opc_style.api.routes_image_jobs import build_background_generator
from opc_style.domain.identity_background_generator import (
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    DeterministicLocalBackgroundGenerator,
)
from opc_style.providers.identity_openai_backgrounds import OpenAIBackgroundGenerator
from opc_style.providers.openai_images import RealImageBudget
from opc_style.settings import RealImageProviderSettings


def _jpeg(color: tuple[int, int, int], size: tuple[int, int] = (1024, 1280)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return output.getvalue()


def _request() -> BackgroundGenerationRequest:
    return BackgroundGenerationRequest(
        style_observations={"lighting": "warm daylight"},
        slot_rules={"surface": "stone"},
        output_ratio="4:5",
    )


def _settings() -> RealImageProviderSettings:
    return RealImageProviderSettings(
        enabled=True,
        api_key="test-secret-value",
        max_run_usd="1.00",
        timeout_seconds=10,
        _env_file=None,
    )


def _payload() -> dict[str, object]:
    return {
        "data": [
            {"b64_json": base64.b64encode(_jpeg(color)).decode("ascii")}
            for color in ((20, 30, 40), (50, 60, 70), (80, 90, 100))
        ],
        "usage": {"total_tokens": 123},
    }


def test_provider_sends_json_without_any_image_bytes_and_crops_to_four_five() -> None:
    """Using image edits or retaining 2:3 output would violate the isolation contract."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload())

    generator = OpenAIBackgroundGenerator(
        _settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("1.00")),
    )

    backgrounds = generator.generate(_request())

    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.openai.com/v1/images/generations"
    body = json.loads(requests[0].content)
    assert body["model"] == "gpt-image-2"
    assert body["n"] == 3
    assert body["size"] == "1024x1280"
    assert "image" not in body
    assert "images" not in body
    assert "reference" not in body
    assert "no jewelry" in body["prompt"].lower()
    assert [(item.width, item.height) for item in backgrounds] == [
        (1024, 1280),
        (1024, 1280),
        (1024, 1280),
    ]
    assert all(item.metadata["provider_size"] == "1024x1280" for item in backgrounds)
    assert all(item.metadata["final_size"] == "1024x1280" for item in backgrounds)


def test_provider_rejects_unsupported_ratio_before_transport() -> None:
    """The browser cannot turn the fixed canvas into arbitrary provider parameters."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_payload())

    generator = OpenAIBackgroundGenerator(
        _settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("1.00")),
    )

    with pytest.raises(BackgroundGenerationError, match="background_ratio_unsupported"):
        generator.generate(
            BackgroundGenerationRequest({}, {}, output_ratio="1:1")
        )

    assert calls == 0


def test_timeout_is_one_uncertain_attempt_and_replay_is_budget_blocked() -> None:
    """A provider timeout must not become an automatic second charge."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("uncertain", request=request)

    generator = OpenAIBackgroundGenerator(
        _settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("1.00")),
    )
    request = _request()

    with pytest.raises(BackgroundGenerationError) as uncertain:
        generator.generate(request)
    assert uncertain.value.code == "background_generation_uncertain"
    assert uncertain.value.outbound_attempts == 1
    with pytest.raises(BackgroundGenerationError, match="background_budget_replay_blocked"):
        generator.generate(request)
    assert calls == 1


def test_runtime_builder_is_local_without_paid_authority() -> None:
    generator = build_background_generator(
        RealImageProviderSettings(enabled=False, _env_file=None)
    )

    assert isinstance(generator, DeterministicLocalBackgroundGenerator)
    assert generator.synthetic_test_only is True


def test_runtime_builder_uses_openai_only_with_explicit_paid_authority() -> None:
    generator = build_background_generator(
        _settings(),
        transport=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, request=request)
            )
        ),
    )

    assert isinstance(generator, OpenAIBackgroundGenerator)
    assert generator.synthetic_test_only is False
