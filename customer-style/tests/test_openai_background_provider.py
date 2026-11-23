from __future__ import annotations

import base64
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from opc_style.domain.background_generator import (
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    BackgroundStyleReference,
)
from opc_style.domain.style_reference_pack import (
    StyleReference,
    reference_manifest_sha256,
)
from opc_style.providers.openai_backgrounds import OpenAIBackgroundGenerator
from opc_style.settings import RealBackgroundProviderSettings


def _png(color: str = "white") -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


def _request(count: int = 2) -> BackgroundGenerationRequest:
    references = tuple(
        BackgroundStyleReference(
            sample_id=uuid4(),
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/png",
        )
        for content in (_png("red"), _png("blue"), _png("green"), _png("yellow"))[
            :count
        ]
    )
    manifest = reference_manifest_sha256(
        tuple(
            StyleReference(
                sample_id=reference.sample_id,
                content_sha256=reference.content_sha256,
                mime_type=reference.mime_type,
            )
            for reference in references
        )
    )
    return BackgroundGenerationRequest(
        style_reference_pack_id=uuid4(),
        style_reference_manifest_sha256=manifest,
        references=references,
        slot_rules={"background": "warm table", "props": "none"},
        style_rules={"lighting": "soft"},
        output_size=(64, 64),
    )


def _settings() -> RealBackgroundProviderSettings:
    return RealBackgroundProviderSettings(
        enabled=True,
        api_key="test-secret",
        max_run_usd=Decimal("0.12"),
    )


def test_background_settings_are_opt_in_and_require_key_and_positive_budget() -> None:
    assert RealBackgroundProviderSettings().enabled is False
    with pytest.raises(ValueError, match="real_background_api_key_required"):
        RealBackgroundProviderSettings(enabled=True, max_run_usd=Decimal("0.12"))
    with pytest.raises(ValueError, match="real_background_budget_required"):
        RealBackgroundProviderSettings(enabled=True, api_key="test", max_run_usd=0)


def test_provider_sends_only_two_to_four_style_references_and_empty_scene_prompt() -> (
    None
):
    generated = _png("tan")
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(generated).decode()}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OpenAIBackgroundGenerator(_settings(), transport=client).generate(
            _request(3)
        )

    body = captured["body"]
    assert body.count(b'name="image[]"') == 3
    assert b"jewelry" in body and b"people" in body and b"hands" in body
    assert b"text" in body and b"brands" in body
    assert b"foreground" not in body and b"product_sha256" not in body
    assert result.content_sha256 == sha256(generated).hexdigest()
    assert result.publishable is False


@pytest.mark.parametrize("count", [0, 1, 5])
def test_provider_rejects_reference_count_before_network(count: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    request = _request(2)
    references = request.references * 3 if count == 5 else request.references[:count]
    request = BackgroundGenerationRequest(
        style_reference_pack_id=request.style_reference_pack_id,
        style_reference_manifest_sha256=request.style_reference_manifest_sha256,
        references=references[:count],
        slot_rules=request.slot_rules,
        style_rules=request.style_rules,
        output_size=request.output_size,
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BackgroundGenerationError, match="style_reference_count_invalid"),
    ):
        OpenAIBackgroundGenerator(_settings(), transport=client).generate(request)
    assert calls == 0


def test_provider_timeout_is_dispatch_uncertain_and_never_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("unknown outcome", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BackgroundGenerationError) as caught,
    ):
        OpenAIBackgroundGenerator(_settings(), transport=client).generate(_request())
    assert caught.value.code == "background_dispatch_uncertain"
    assert caught.value.outbound_attempts == 1
    assert calls == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "background_provider_rejected"),
        (401, "background_provider_auth_failed"),
        (429, "background_provider_rate_limited"),
        (500, "background_provider_failed"),
    ],
)
def test_provider_maps_errors_without_retry(status: int, code: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BackgroundGenerationError) as caught,
    ):
        OpenAIBackgroundGenerator(_settings(), transport=client).generate(_request())
    assert caught.value.code == code
    assert caught.value.outbound_attempts == 1
    assert calls == 1


@pytest.mark.parametrize(
    ("provider_code", "domain_code"),
    [
        ("credit_balance_exhausted", "background_provider_credit_exhausted"),
        (
            "organization_spend_limit_exceeded",
            "background_provider_organization_spend_limit_exceeded",
        ),
        (
            "project_spend_limit_exceeded",
            "background_provider_project_spend_limit_exceeded",
        ),
        (
            "organization_usage_limit_exceeded",
            "background_provider_organization_usage_limit_exceeded",
        ),
        ("slow_down", "background_provider_slow_down"),
        ("unrecognized_code", "background_provider_rate_limited"),
    ],
)
def test_provider_preserves_safe_429_reason_without_retry(
    provider_code: str, domain_code: str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"x-request-id": "req-test", "retry-after": "10"},
            json={"error": {"code": provider_code, "message": "not logged"}},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BackgroundGenerationError) as caught,
    ):
        OpenAIBackgroundGenerator(_settings(), transport=client).generate(_request())
    assert caught.value.code == domain_code
    assert caught.value.outbound_attempts == 1
    assert calls == 1


def test_provider_rejects_invalid_returned_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(b"not-image").decode()}]},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BackgroundGenerationError, match="background_image_invalid"),
    ):
        OpenAIBackgroundGenerator(_settings(), transport=client).generate(_request())


def test_provider_rejects_changed_reference_manifest_before_network() -> None:
    request = _request()
    changed = BackgroundGenerationRequest(
        style_reference_pack_id=request.style_reference_pack_id,
        style_reference_manifest_sha256="0" * 64,
        references=request.references,
        slot_rules=request.slot_rules,
        style_rules=request.style_rules,
        output_size=request.output_size,
    )
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            BackgroundGenerationError, match="style_reference_manifest_mismatch"
        ),
    ):
        OpenAIBackgroundGenerator(_settings(), transport=client).generate(changed)
    assert calls == 0


def test_provider_rejects_product_digest_hidden_inside_style_rules() -> None:
    request = _request()
    unsafe = BackgroundGenerationRequest(
        style_reference_pack_id=request.style_reference_pack_id,
        style_reference_manifest_sha256=request.style_reference_manifest_sha256,
        references=request.references,
        slot_rules={"foreground_sha256": "f" * 64},
        style_rules=request.style_rules,
        output_size=request.output_size,
    )
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(BackgroundGenerationError, match="background_rule_scope_invalid"),
    ):
        OpenAIBackgroundGenerator(_settings(), transport=client).generate(unsafe)
    assert calls == 0
