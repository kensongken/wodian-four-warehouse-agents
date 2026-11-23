"""No-network tests for the replaceable product identity analyzer."""

import json
from hashlib import sha256
from uuid import UUID

import httpx
import pytest

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_identity import ProductIdentityImage
from opc_style.providers.openai_product_identity import OpenAIProductIdentityAnalyzer
from opc_style.settings import RealProductIdentitySettings


def identity_image() -> ProductIdentityImage:
    content = b"bounded-test-image"
    return ProductIdentityImage(
        asset_id=UUID("00000000-0000-0000-0000-000000000123"),
        product_id="test-product-bracelet",
        content=content,
        content_sha256=sha256(content).hexdigest(),
        mime_type="image/jpeg",
    )


def valid_identity_payload() -> dict[str, object]:
    return {
        "category": {"value": "bracelet", "certainty": "observed"},
        "primary_colors": {"value": ["green", "gold_tone"], "certainty": "observed"},
        "visible_surface_appearance": {
            "value": ["translucent", "glossy"],
            "certainty": "observed",
        },
        "component_shapes": {"value": ["round_bead"], "certainty": "observed"},
        "visible_component_count": {"value": 18, "certainty": "observed"},
        "component_arrangement": {"value": "single_strand", "certainty": "observed"},
        "core_accessories": {
            "value": [{"kind": "clasp", "visible_count": 1}],
            "certainty": "observed",
        },
        "overall_form": {"value": "closed_loop", "certainty": "observed"},
        "relative_proportions": {"value": "珠体大小近似一致", "certainty": "observed"},
        "distinguishing_marks": {"value": "扣件旁可见一枚圆形吊饰", "certainty": "observed"},
        "visibility_limits": {"value": ["clasp_hidden"], "certainty": "observed"},
    }


def provider_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(payload, ensure_ascii=False),
                        }
                    ],
                }
            ],
        },
    )


def settings(**overrides: object) -> RealProductIdentitySettings:
    values: dict[str, object] = {
        "enabled": True,
        "api_key": "test-secret",
        "max_run_usd": "1.00",
    }
    values.update(overrides)
    return RealProductIdentitySettings(_env_file=None, **values)


def analyzer_returning(
    response: httpx.Response,
    calls: list[httpx.Request],
    *,
    analyzer_settings: RealProductIdentitySettings | None = None,
) -> OpenAIProductIdentityAnalyzer:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    return OpenAIProductIdentityAnalyzer(
        analyzer_settings or settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_provider_sends_one_image_store_false_and_strict_schema() -> None:
    calls: list[httpx.Request] = []
    analyzer = analyzer_returning(provider_response(valid_identity_payload()), calls)

    draft = analyzer.analyze(identity_image())

    assert analyzer.analysis_mode == "real_product_identity_proposal"
    assert draft.category.value == "bracelet"
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["store"] is False
    assert "Carrier presence alone is not partially_occluded" in body["instructions"]
    assert "physically hides product pixels" in body["instructions"]
    assert body["text"]["format"]["strict"] is True
    assert sum(
        part["type"] == "input_image" for part in body["input"][0]["content"]
    ) == 1
    assert body["input"][0]["content"][0]["image_url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert "test-secret" not in json.dumps(body)
    schema_text = json.dumps(body["text"]["format"]["schema"])
    for unsupported in (
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
    ):
        assert unsupported not in schema_text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["primary_colors"].update(  # type: ignore[union-attr]
            value=["green", "green"]
        ),
        lambda payload: payload["category"].update(  # type: ignore[union-attr]
            value="watch"
        ),
    ],
)
def test_provider_rejects_duplicate_or_unknown_values_locally(mutate: object) -> None:
    payload = valid_identity_payload()
    mutate(payload)  # type: ignore[operator]
    analyzer = analyzer_returning(provider_response(payload), [])

    with pytest.raises(
        DomainRuleError, match="product_identity_analysis_invalid_response"
    ):
        analyzer.analyze(identity_image())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"status": "incomplete", "output": []}),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "{}"},
                            {"type": "output_text", "text": "{}"},
                        ],
                    }
                ],
            },
        ),
    ],
)
def test_invalid_provider_response_fails_closed(response: httpx.Response) -> None:
    analyzer = analyzer_returning(response, [])

    with pytest.raises(
        DomainRuleError, match="product_identity_analysis_invalid_response"
    ):
        analyzer.analyze(identity_image())


def test_provider_http_failure_is_bounded_and_excludes_message() -> None:
    analyzer = analyzer_returning(
        httpx.Response(
            400,
            json={
                "error": {
                    "type": "Invalid Request!!!",
                    "code": "Schema/Unsupported",
                    "message": "private provider detail must never escape",
                }
            },
        ),
        [],
    )

    with pytest.raises(DomainRuleError) as captured:
        analyzer.analyze(identity_image())

    assert str(captured.value) == (
        "product_identity_provider_http_400_invalid_request_schema_unsupported"
    )
    assert "private" not in str(captured.value)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("late"), httpx.ConnectError("down")])
def test_timeout_or_network_failure_is_unavailable(failure: httpx.HTTPError) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    analyzer = OpenAIProductIdentityAnalyzer(
        settings(),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(
        DomainRuleError, match="^product_identity_analysis_unavailable$"
    ):
        analyzer.analyze(identity_image())


def test_settings_require_explicit_key_and_positive_budget_when_enabled() -> None:
    disabled = RealProductIdentitySettings(_env_file=None)
    assert disabled.enabled is False
    assert disabled.model == "gpt-4.1-mini-2025-04-14"
    assert disabled.timeout_seconds == 60

    for values, error in (
        (
            {"enabled": True, "api_key": "   ", "max_run_usd": "1"},
            "real_product_identity_api_key_required",
        ),
        (
            {"enabled": True, "api_key": "test-secret", "max_run_usd": "0"},
            "real_product_identity_budget_required",
        ),
    ):
        with pytest.raises(ValueError, match=error):
            RealProductIdentitySettings(_env_file=None, **values)


def test_settings_accept_exact_environment_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REAL_PRODUCT_IDENTITY_ENABLED", "true")
    monkeypatch.setenv("REAL_PRODUCT_IDENTITY_API_KEY", "test-env-secret")
    monkeypatch.setenv("REAL_PRODUCT_IDENTITY_MODEL", "identity-model-snapshot")
    monkeypatch.setenv("REAL_PRODUCT_IDENTITY_MAX_RUN_USD", "2.50")
    monkeypatch.setenv("REAL_PRODUCT_IDENTITY_TIMEOUT_SECONDS", "45")

    configured = RealProductIdentitySettings(_env_file=None)

    assert configured.enabled is True
    assert configured.api_key is not None
    assert configured.api_key.get_secret_value() == "test-env-secret"
    assert configured.model == "identity-model-snapshot"
    assert configured.max_run_usd == 2.5
    assert configured.timeout_seconds == 45


def test_analyzer_copies_settings_and_fixes_model_for_lifetime() -> None:
    configured = settings(model="identity-model-v1")
    calls: list[httpx.Request] = []
    analyzer = analyzer_returning(
        provider_response(valid_identity_payload()),
        calls,
        analyzer_settings=configured,
    )
    configured.model = "identity-model-v2"

    analyzer.analyze(identity_image())

    assert json.loads(calls[0].content)["model"] == "identity-model-v1"
