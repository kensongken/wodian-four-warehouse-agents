"""HTTP boundary for paid identity proposals and human-confirmed cards."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

import opc_style.api.routes_local_style_suite as local_routes
from opc_style.api.dependencies import get_session
from opc_style.db import create_database_engine
from opc_style.domain import (
    ProductIdentityDraft,
    ProductIdentityImage,
    normalize_identity_draft,
)
from opc_style.main import create_app
from opc_style.models import Base
from opc_style.settings import RealProductIdentitySettings

CUSTOMER_A = UUID("00000000-0000-0000-0000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-0000-0000-0000000000b2")
MODEL = "recording-product-identity-v1"
MAXIMUM_USD = "0.125000"


def _identity_payload() -> dict[str, object]:
    return {
        "category": {"value": "bracelet", "certainty": "observed"},
        "primary_colors": {
            "value": ["green", "gold_tone"],
            "certainty": "observed",
        },
        "visible_surface_appearance": {
            "value": ["translucent", "glossy"],
            "certainty": "observed",
        },
        "component_shapes": {
            "value": ["round_bead"],
            "certainty": "observed",
        },
        "visible_component_count": {"value": 18, "certainty": "observed"},
        "component_arrangement": {
            "value": "single_strand",
            "certainty": "observed",
        },
        "core_accessories": {
            "value": [{"kind": "clasp", "visible_count": 1}],
            "certainty": "observed",
        },
        "overall_form": {"value": "closed_loop", "certainty": "observed"},
        "relative_proportions": {
            "value": "主体珠大小接近，扣头明显更小",
            "certainty": "observed",
        },
        "distinguishing_marks": {
            "value": "一颗主体珠有明显深色斑点",
            "certainty": "observed",
        },
        "visibility_limits": {
            "value": ["clasp_hidden"],
            "certainty": "observed",
        },
    }


class RecordingIdentityAnalyzer:
    analyzer_version = MODEL
    analysis_mode = "real_product_identity_proposal"

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls = 0
        self.images: list[ProductIdentityImage] = []
        self._failure = failure

    def analyze(self, image: ProductIdentityImage) -> ProductIdentityDraft:
        self.calls += 1
        self.images.append(image)
        if self._failure is not None:
            raise self._failure
        return normalize_identity_draft(_identity_payload())


def _headers(customer_id: UUID = CUSTOMER_A) -> dict[str, str]:
    return {"X-Customer-Id": str(customer_id)}


@contextmanager
def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    analyzer: RecordingIdentityAnalyzer | None,
    enabled: bool = True,
    maximum_usd: str = MAXIMUM_USD,
) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv(
        "LOCAL_STYLE_ASSET_ROOT", str(tmp_path / f"workspace-{uuid4().hex}")
    )
    monkeypatch.setenv("REAL_PRODUCT_IDENTITY_ENABLED", "false")
    monkeypatch.delenv("REAL_PRODUCT_IDENTITY_API_KEY", raising=False)
    monkeypatch.delenv("REAL_PRODUCT_IDENTITY_MAX_RUN_USD", raising=False)

    engine = create_database_engine(f"sqlite:///{tmp_path / f'{uuid4().hex}.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    app = create_app()
    app.state.session_factory = sessions
    app.state.real_product_identity_settings = RealProductIdentitySettings(
        enabled=enabled,
        api_key="test-only-key" if enabled else None,
        model=MODEL,
        max_run_usd=maximum_usd if enabled else "0",
        _env_file=None,
    )
    app.state.real_product_identity_analyzer = analyzer

    def session_override() -> Generator[Session, None, None]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        engine.dispose()


def _upload_product(
    client: TestClient, *, customer_id: UUID = CUSTOMER_A
) -> dict[str, Any]:
    image = BytesIO()
    Image.new("RGB", (24, 18), (20, 80, 120)).save(image, format="PNG")
    token = uuid4().hex
    response = client.post(
        "/local/style-suite/api/product",
        headers={
            **_headers(customer_id),
            "X-Product-Id": f"identity-product-{token}",
            "X-Photo-Id": f"identity-photo-{token}",
            "Content-Type": "image/png",
        },
        content=image.getvalue(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _preview(client: TestClient, asset: dict[str, Any]) -> dict[str, Any]:
    response = client.get(
        f"/local/style-suite/api/assets/{asset['id']}/identity-analysis-preview",
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _execute(
    client: TestClient,
    asset: dict[str, Any],
    preview: dict[str, Any],
    *,
    key: str = "browser-identity-1",
):
    return client.post(
        f"/local/style-suite/api/assets/{asset['id']}/identity-analysis-runs",
        headers=_headers(),
        json={
            "idempotency_key": key,
            "confirmed_maximum_usd": preview["maximum_usd"],
            "confirmed_manifest_sha256": preview["manifest_sha256"],
        },
    )


def test_identity_api_preview_execute_confirm_and_list_replay_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = RecordingIdentityAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        asset = _upload_product(client)
        preview = _preview(client, asset)
        first = _execute(client, asset, preview)
        replay = _execute(client, asset, preview)
        assert first.status_code == 201, first.text
        assert replay.status_code == 200, replay.text
        assert first.json() == replay.json() | {"created": True}
        proposal = first.json()["proposal"]

        confirmed = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-card-versions",
            headers=_headers(),
            json={
                "analysis_run_id": first.json()["run_id"],
                "card": proposal,
                "confirmed": True,
            },
        )
        confirmed_replay = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-card-versions",
            headers=_headers(),
            json={
                "analysis_run_id": first.json()["run_id"],
                "card": proposal,
                "confirmed": True,
            },
        )
        versions = client.get(
            f"/local/style-suite/api/assets/{asset['id']}/identity-card-versions",
            headers=_headers(),
        )

    assert set(preview) == {
        "asset_id",
        "product_id",
        "source_content_sha256",
        "model",
        "maximum_usd",
        "currency",
        "input_image_count",
        "manifest_sha256",
        "publishable",
        "reconciliation_required",
    }
    assert preview["asset_id"] == asset["id"]
    assert preview["model"] == MODEL
    assert preview["input_image_count"] == 1
    assert preview["publishable"] is False
    assert preview["reconciliation_required"] is False
    assert set(first.json()) == {"run_id", "proposal", "created"}
    assert first.json()["created"] is True
    assert replay.json()["created"] is False
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed_replay.status_code == 200, confirmed_replay.text
    assert confirmed.json()["id"] == confirmed_replay.json()["id"]
    assert confirmed.json()["version"] == 1
    assert confirmed.json()["card"] == proposal
    assert confirmed.json()["created"] is True
    assert confirmed_replay.json()["created"] is False
    assert versions.status_code == 200, versions.text
    assert len(versions.json()) == 1
    assert versions.json()[0]["id"] == confirmed.json()["id"]
    assert versions.json()[0]["created"] is False
    assert analyzer.calls == 1
    assert len(analyzer.images) == 1


def test_disabled_provider_is_stable_409_and_never_calls_analyzer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = RecordingIdentityAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer, enabled=False) as client:
        asset = _upload_product(client)
        response = client.get(
            f"/local/style-suite/api/assets/{asset['id']}/identity-analysis-preview",
            headers=_headers(),
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "product_identity_analysis_provider_disabled",
            "retryable": False,
        }
    }
    assert analyzer.calls == 0


def test_disabled_provider_allows_zero_cost_manual_identity_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = RecordingIdentityAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer, enabled=False) as client:
        asset = _upload_product(client)
        first = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-manual-drafts",
            headers=_headers(),
            json={"idempotency_key": "manual-card-1"},
        )
        replay = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-manual-drafts",
            headers=_headers(),
            json={"idempotency_key": "manual-card-1"},
        )
        incomplete = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-card-versions",
            headers=_headers(),
            json={
                "analysis_run_id": first.json()["run_id"],
                "card": first.json()["proposal"],
                "confirmed": True,
            },
        )
        confirmed = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-card-versions",
            headers=_headers(),
            json={
                "analysis_run_id": first.json()["run_id"],
                "card": _identity_payload(),
                "confirmed": True,
            },
        )

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert first.json() == replay.json() | {"created": True}
    assert all(
        observation == {"value": None, "certainty": "unclear"}
        for observation in first.json()["proposal"].values()
    )
    assert incomplete.status_code == 409
    assert incomplete.json()["error"]["code"] == "product_identity_card_incomplete"
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["card"] == _identity_payload()
    assert analyzer.calls == 0


def test_identity_requests_are_strict_and_zero_cost_reaches_domain_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_fixture = "s" + "k-" + "secret-should-not-echo"
    analyzer = RecordingIdentityAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        asset = _upload_product(client)
        preview = _preview(client, asset)
        extra = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-analysis-runs",
            headers=_headers(),
            json={
                "idempotency_key": "strict-extra",
                "confirmed_maximum_usd": preview["maximum_usd"],
                "confirmed_manifest_sha256": preview["manifest_sha256"],
                "api_key": secret_fixture,
            },
        )
        zero = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-analysis-runs",
            headers=_headers(),
            json={
                "idempotency_key": "zero-cost",
                "confirmed_maximum_usd": "0",
                "confirmed_manifest_sha256": preview["manifest_sha256"],
            },
        )

    assert extra.status_code == 422
    assert extra.json() == {
        "error": {"code": "product_identity_request_invalid", "retryable": False}
    }
    assert secret_fixture not in extra.text
    assert zero.status_code == 409
    assert zero.json() == {
        "error": {
            "code": "product_identity_analysis_confirmation_required",
            "retryable": False,
        }
    }
    assert analyzer.calls == 0


def test_malformed_identity_card_validation_never_echoes_secret_or_local_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_fixture = "s" + "k-" + "card-secret"
    analyzer = RecordingIdentityAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        asset = _upload_product(client)
        preview = _preview(client, asset)
        analyzed = _execute(client, asset, preview, key="malformed-card")
        assert analyzed.status_code == 201, analyzed.text
        malformed_card = analyzed.json()["proposal"] | {
            "unexpected": {
                "api_key": secret_fixture,
                "storage_path": "/Users/test-user/product.png",
            }
        }
        response = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-card-versions",
            headers=_headers(),
            json={
                "analysis_run_id": analyzed.json()["run_id"],
                "card": malformed_card,
                "confirmed": True,
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "product_identity_request_invalid", "retryable": False}
    }
    assert secret_fixture not in response.text
    assert "/Users/test-user/product.png" not in response.text
    assert analyzer.calls == 1


def test_disabled_provider_still_allows_confirming_and_listing_durable_cards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = RecordingIdentityAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        asset = _upload_product(client)
        preview = _preview(client, asset)
        analyzed = _execute(client, asset, preview, key="disable-after-analysis")
        assert analyzed.status_code == 201, analyzed.text

        client.app.state.real_product_identity_settings = RealProductIdentitySettings(
            enabled=False, _env_file=None
        )
        client.app.state.real_product_identity_analyzer = None
        confirmed = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-card-versions",
            headers=_headers(),
            json={
                "analysis_run_id": analyzed.json()["run_id"],
                "card": analyzed.json()["proposal"],
                "confirmed": True,
            },
        )
        versions = client.get(
            f"/local/style-suite/api/assets/{asset['id']}/identity-card-versions",
            headers=_headers(),
        )
        disabled_preview = client.get(
            f"/local/style-suite/api/assets/{asset['id']}/identity-analysis-preview",
            headers=_headers(),
        )

    assert confirmed.status_code == 201, confirmed.text
    assert versions.status_code == 200, versions.text
    assert [row["id"] for row in versions.json()] == [confirmed.json()["id"]]
    assert disabled_preview.status_code == 409
    assert disabled_preview.json() == {
        "error": {
            "code": "product_identity_analysis_provider_disabled",
            "retryable": False,
        }
    }
    assert analyzer.calls == 1


def test_identity_request_accepts_the_exact_startup_budget_without_api_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = RecordingIdentityAnalyzer()
    with _client(
        monkeypatch,
        tmp_path,
        analyzer=analyzer,
        maximum_usd="2.50",
    ) as client:
        asset = _upload_product(client)
        preview = _preview(client, asset)
        response = _execute(client, asset, preview, key="configured-budget")

    assert response.status_code == 201, response.text
    assert analyzer.calls == 1


def test_missing_and_cross_customer_identity_scopes_are_indistinguishable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = RecordingIdentityAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        asset_a = _upload_product(client)
        asset_b = _upload_product(client, customer_id=CUSTOMER_B)
        preview = _preview(client, asset_a)
        analyzed = _execute(client, asset_a, preview, key="tenant-scope")
        assert analyzed.status_code == 201, analyzed.text

        foreign_asset_preview = client.get(
            f"/local/style-suite/api/assets/{asset_a['id']}/identity-analysis-preview",
            headers=_headers(CUSTOMER_B),
        )
        absent_asset_preview = client.get(
            f"/local/style-suite/api/assets/{uuid4()}/identity-analysis-preview",
            headers=_headers(CUSTOMER_B),
        )
        foreign_card_list = client.get(
            f"/local/style-suite/api/assets/{asset_a['id']}/identity-card-versions",
            headers=_headers(CUSTOMER_B),
        )
        absent_card_list = client.get(
            f"/local/style-suite/api/assets/{uuid4()}/identity-card-versions",
            headers=_headers(CUSTOMER_B),
        )
        foreign_run = client.post(
            f"/local/style-suite/api/assets/{asset_b['id']}/identity-card-versions",
            headers=_headers(CUSTOMER_B),
            json={
                "analysis_run_id": analyzed.json()["run_id"],
                "card": analyzed.json()["proposal"],
                "confirmed": True,
            },
        )
        absent_run = client.post(
            f"/local/style-suite/api/assets/{asset_b['id']}/identity-card-versions",
            headers=_headers(CUSTOMER_B),
            json={
                "analysis_run_id": str(uuid4()),
                "card": analyzed.json()["proposal"],
                "confirmed": True,
            },
        )

    unavailable = {
        "error": {"code": "product_identity_asset_unavailable", "retryable": False}
    }
    for response in (
        foreign_asset_preview,
        absent_asset_preview,
        foreign_card_list,
        absent_card_list,
        foreign_run,
        absent_run,
    ):
        assert response.status_code == 409
        assert response.json() == unavailable
    assert analyzer.calls == 1


def test_uncertain_analysis_error_is_sanitized_and_never_redispatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = RecordingIdentityAnalyzer(
        failure=RuntimeError("provider body, api key, and local path must not escape")
    )
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        asset = _upload_product(client)
        preview = _preview(client, asset)
        first = _execute(client, asset, preview, key="sanitized-failure")
        replay = _execute(client, asset, preview, key="sanitized-failure")
        recovered_preview = _preview(client, asset)

    expected = {
        "error": {
            "code": "product_identity_analysis_outcome_uncertain",
            "retryable": False,
        }
    }
    assert first.status_code == 409
    assert replay.status_code == 409
    assert first.json() == expected
    assert replay.json() == expected
    assert recovered_preview["reconciliation_required"] is True
    assert analyzer.calls == 1
    assert "provider" not in first.text
    assert "api key" not in first.text
    assert "local path" not in first.text


def test_local_startup_owns_disabled_identity_settings_without_building_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider_constructors = 0

    def fail_if_constructed(*args: object, **kwargs: object) -> None:
        nonlocal provider_constructors
        provider_constructors += 1
        raise AssertionError("disabled startup must not construct a provider")

    monkeypatch.setattr(
        local_routes,
        "OpenAIProductIdentityAnalyzer",
        fail_if_constructed,
        raising=False,
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(tmp_path / "startup-workspace"))
    monkeypatch.setenv("REAL_PRODUCT_IDENTITY_ENABLED", "false")
    monkeypatch.delenv("REAL_PRODUCT_IDENTITY_API_KEY", raising=False)
    monkeypatch.delenv("REAL_PRODUCT_IDENTITY_MAX_RUN_USD", raising=False)

    app = create_app()
    try:
        assert isinstance(
            app.state.real_product_identity_settings, RealProductIdentitySettings
        )
        assert app.state.real_product_identity_settings.enabled is False
        assert app.state.real_product_identity_analyzer is None
        assert provider_constructors == 0
    finally:
        app.state.classification_bridge_store.close()
        app.state.local_demo_sandbox.close()
