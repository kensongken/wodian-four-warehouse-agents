"""Only mocked HTTP crosses the guarded visual-provider boundary."""

import base64
import json
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError
from test_style_visual_analysis import png

from opc_style import settings as settings_module
from opc_style.domain import analyzer as contracts
from opc_style.domain.errors import DomainRuleError


def setup_adapter(handler, budget="1", **overrides):
    from opc_style.providers.openai_style_analysis import OpenAIVisualStyleAnalyzer

    settings = settings_module.RealStyleAnalysisSettings(
        enabled=True, api_key="mock-test-key", max_run_usd=Decimal(budget), **overrides
    )
    return OpenAIVisualStyleAnalyzer(
        settings, transport=httpx.Client(transport=httpx.MockTransport(handler))
    )


def sample():
    data = png()
    return contracts.VisualStyleSample(
        uuid4(), data, sha256(data).hexdigest(), "image/png"
    )


def result(sample_id):
    return {
        "groups": [
            {
                "proposal_key": "group-1",
                "suggested_name": "red landscape",
                "evidence_sample_ids": [str(sample_id)],
                "observations": {"palette": ["red"], "aspect_ratio": ["landscape"]},
                "suggested_slots": ["hero"],
            }
        ]
    }


def response(body):
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(body)}],
                }
            ],
            "usage": {"input_tokens": 500, "output_tokens": 100},
        },
    )


def test_provider_uses_inline_bytes_strict_schema_and_preserves_evidence():
    assert hasattr(contracts, "VisualStyleSample")
    image = sample()

    def handler(request):
        payload = json.loads(request.content)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert payload["store"] is False
        assert payload["text"]["format"]["strict"] is True
        image_part = payload["input"][0]["content"][1]
        assert image_part["image_url"].startswith("data:image/jpeg;base64,")
        assert str(image.sample_id) in json.dumps(payload)
        return response(result(image.sample_id))

    groups = setup_adapter(handler).analyze((image,))
    assert groups[0].evidence_sample_ids == (image.sample_id,)
    assert groups[0].observations == {
        "palette": ("red",),
        "aspect_ratio": ("landscape",),
    }


def test_provider_sends_bounded_analysis_surrogate_without_mutating_original():
    original_buffer = BytesIO()
    Image.new("RGB", (1600, 1200), (80, 120, 160)).save(
        original_buffer, format="JPEG", quality=100
    )
    original = original_buffer.getvalue()
    image = contracts.VisualStyleSample(
        uuid4(), original, sha256(original).hexdigest(), "image/jpeg"
    )

    def handler(request):
        payload = json.loads(request.content)
        image_url = payload["input"][0]["content"][1]["image_url"]
        encoded = image_url.removeprefix("data:image/jpeg;base64,")
        surrogate = base64.b64decode(encoded)
        with Image.open(BytesIO(surrogate)) as decoded:
            assert max(decoded.size) == 768
            assert decoded.format == "JPEG"
        assert len(surrogate) < len(original)
        assert image.content == original
        assert sha256(image.content).hexdigest() == image.content_sha256
        return response(result(image.sample_id))

    setup_adapter(handler).analyze((image,))


@pytest.mark.parametrize(
    "mode",
    [
        "malformed_json",
        "unknown_evidence",
        "forbidden_field",
        "forbidden_claim",
        "refusal",
        "incomplete",
    ],
)
def test_untrusted_provider_output_fails_closed(mode):
    assert hasattr(contracts, "VisualStyleSample")
    image = sample()
    body = result(image.sample_id)
    if mode == "unknown_evidence":
        body["groups"][0]["evidence_sample_ids"] = [str(uuid4())]
    if mode == "forbidden_field":
        body["groups"][0]["observations"]["material"] = ["gold"]
    if mode == "forbidden_claim":
        body["groups"][0]["observations"]["palette"] = ["authentic gold worth $100"]

    def handler(request):
        if mode == "malformed_json":
            return httpx.Response(200, content=b"not-json")
        if mode in {"refusal", "incomplete"}:
            return httpx.Response(200, json={"status": mode, "output": []})
        return response(body)

    with pytest.raises(DomainRuleError, match="style_visual_analysis_invalid_response"):
        setup_adapter(handler).analyze((image,))


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authorization"),
        (403, "authorization"),
        (429, "rate_limited"),
        (500, "unavailable"),
    ],
)
def test_provider_http_errors_are_stable(status, code):
    assert hasattr(contracts, "VisualStyleSample")
    with pytest.raises(DomainRuleError, match="style_visual_analysis_" + code):
        setup_adapter(lambda request: httpx.Response(status)).analyze((sample(),))


def test_timeout_is_stable_and_budget_is_reserved_before_network():
    assert hasattr(contracts, "VisualStyleSample")
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout")

    with pytest.raises(DomainRuleError, match="style_visual_analysis_timeout"):
        setup_adapter(handler).analyze((sample(),))
    with pytest.raises(DomainRuleError, match="style_visual_analysis_budget_exhausted"):
        setup_adapter(handler, budget="0.000001").analyze((sample(),))
    assert len(calls) == 1


def test_settings_disabled_zero_and_enable_requires_credential_and_budget(monkeypatch):
    assert hasattr(settings_module, "RealStyleAnalysisSettings")
    for key in ("ENABLED", "API_KEY", "MODEL", "MAX_RUN_USD"):
        monkeypatch.delenv("REAL_STYLE_ANALYSIS_" + key, raising=False)
    settings = settings_module.RealStyleAnalysisSettings(_env_file=None)
    assert not settings.enabled and settings.max_run_usd == 0
    for values in ({"enabled": True}, {"enabled": True, "api_key": "mock"}):
        with pytest.raises(ValidationError):
            settings_module.RealStyleAnalysisSettings(_env_file=None, **values)


def test_repeated_requests_share_budget_and_timeout_keeps_reservation():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout")

    adapter = setup_adapter(handler, budget="0.5")
    for expected in ("timeout", "budget_exhausted"):
        with pytest.raises(DomainRuleError, match="style_visual_analysis_" + expected):
            adapter.analyze((sample(),))
    assert len(calls) == 1


def test_unknown_model_is_rejected_before_any_request():
    with pytest.raises(DomainRuleError, match="style_visual_analysis_model_unpriced"):
        setup_adapter(
            lambda request: pytest.fail("must not send"), model="unknown-unpriced-model"
        )


def test_provider_error_via_service_never_creates_style_version(session, tmp_path):
    from sqlalchemy import func, select
    from test_style_visual_analysis import seal, service

    from opc_style.models import StyleCardVersion
    from opc_style.services.real_photos import LocalSyntheticAssetStore

    customer_id = uuid4()
    with LocalSyntheticAssetStore(tmp_path / "media") as store:
        seal(session, store, customer_id)
        adapter = setup_adapter(lambda request: httpx.Response(401))
        with pytest.raises(
            DomainRuleError, match="style_visual_analysis_authorization"
        ):
            service(session, store, adapter).propose(customer_id)
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0
