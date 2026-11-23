"""Sanitized HTTP boundary for reusable product-identity observations."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_product_identity_api import (
    CUSTOMER_B,
    RecordingIdentityAnalyzer,
    _client,
    _execute,
    _headers,
    _identity_payload,
    _preview,
    _upload_product,
)

from opc_style.domain.product_identity import (
    ProductIdentityDraft,
    ProductIdentityImage,
    normalize_identity_draft,
)


def _clear_payload(**overrides: object) -> dict[str, object]:
    payload = _identity_payload()
    payload["visibility_limits"] = {"value": [], "certainty": "observed"}
    payload.update(overrides)
    return payload


class MutableObservationAnalyzer(RecordingIdentityAnalyzer):
    def __init__(self) -> None:
        super().__init__()
        self.payload = _clear_payload()
        self.failure: Exception | None = None

    def analyze(self, image: ProductIdentityImage) -> ProductIdentityDraft:
        self.calls += 1
        self.images.append(image)
        if self.failure is not None:
            raise self.failure
        return normalize_identity_draft(self.payload)


def _upload_same_product(client: TestClient, product_id: str) -> dict[str, Any]:
    image = BytesIO()
    Image.new("RGB", (20, 20), (120, 30, 40)).save(image, format="PNG")
    token = uuid4().hex
    response = client.post(
        "/local/style-suite/api/product",
        headers={
            **_headers(),
            "X-Product-Id": product_id,
            "X-Photo-Id": f"observation-{token}",
            "Content-Type": "image/png",
        },
        content=image.getvalue(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _card_and_target(
    client: TestClient, analyzer: MutableObservationAnalyzer
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = _upload_product(client)
    identity_preview = _preview(client, source)
    analyzed = _execute(client, source, identity_preview, key="source-card")
    assert analyzed.status_code == 201, analyzed.text
    card = client.post(
        f"/local/style-suite/api/assets/{source['id']}/identity-card-versions",
        headers=_headers(),
        json={
            "analysis_run_id": analyzed.json()["run_id"],
            "card": analyzed.json()["proposal"],
            "confirmed": True,
        },
    )
    assert card.status_code == 201, card.text
    target = _upload_same_product(client, source["product_id"])
    analyzer.calls = 0
    analyzer.images.clear()
    return card.json(), target


def _observation_preview(
    client: TestClient, target: dict[str, Any], card: dict[str, Any]
):
    return client.post(
        f"/local/style-suite/api/assets/{target['id']}/identity-observations/preview",
        headers=_headers(),
        json={"card_version_id": card["id"]},
    )


def _observe(
    client: TestClient,
    target: dict[str, Any],
    card: dict[str, Any],
    preview: dict[str, Any],
    *,
    key: str,
):
    return client.post(
        f"/local/style-suite/api/assets/{target['id']}/identity-observation-runs",
        headers=_headers(),
        json={
            "card_version_id": card["id"],
            "idempotency_key": key,
            "confirmed_maximum_usd": preview["maximum_usd"],
            "confirmed_manifest_sha256": preview["manifest_sha256"],
        },
    )


def test_pass_observation_preview_execute_replay_and_current_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = MutableObservationAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        card, target = _card_and_target(client, analyzer)
        preview_response = _observation_preview(client, target, card)
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        first = _observe(client, target, card, preview, key="observe-pass")
        replay = _observe(client, target, card, preview, key="observe-pass")
        current = client.get(
            f"/local/style-suite/api/assets/{target['id']}/identity-observations/current",
            headers=_headers(),
            params={"card_version_id": card["id"]},
        )

    assert set(preview) == {
        "asset_id",
        "product_id",
        "source_content_sha256",
        "product_identity_card_version_id",
        "product_identity_card_sha256",
        "model",
        "observer_version",
        "policy_version",
        "maximum_usd",
        "currency",
        "input_image_count",
        "manifest_sha256",
        "publishable",
    }
    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["decision"] == "pass"
    assert first.json()["reason_codes"] == []
    assert replay.json() == first.json() | {"created": False}
    assert current.status_code == 200, current.text
    assert current.json() == first.json() | {"created": False}
    assert analyzer.calls == 1


def test_card_source_photo_uses_zero_cost_local_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = MutableObservationAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        source = _upload_product(client)
        identity_preview = _preview(client, source)
        analyzed = _execute(client, source, identity_preview, key="source-local-card")
        card_response = client.post(
            f"/local/style-suite/api/assets/{source['id']}/identity-card-versions",
            headers=_headers(),
            json={
                "analysis_run_id": analyzed.json()["run_id"],
                "card": analyzed.json()["proposal"],
                "confirmed": True,
            },
        )
        card = card_response.json()
        analyzer.calls = 0

        preview_response = _observation_preview(client, source, card)
        preview = preview_response.json()
        observed = _observe(
            client,
            source,
            card,
            preview,
            key="observe-source-locally",
        )

    assert preview_response.status_code == 200, preview_response.text
    assert preview["maximum_usd"] == "0.000000"
    assert preview["input_image_count"] == 0
    assert observed.status_code == 201, observed.text
    assert observed.json()["decision"] == "pass"
    assert observed.json()["reason_codes"] == ["card_source_exact_match"]
    assert analyzer.calls == 0


def test_manual_card_source_stays_locally_verifiable_when_provider_is_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = MutableObservationAnalyzer()
    with _client(
        monkeypatch, tmp_path, analyzer=analyzer, enabled=False
    ) as client:
        source = _upload_product(client)
        draft = client.post(
            f"/local/style-suite/api/assets/{source['id']}/identity-manual-drafts",
            headers=_headers(),
            json={"idempotency_key": "manual-local-observation"},
        )
        assert draft.status_code == 201, draft.text
        card_response = client.post(
            f"/local/style-suite/api/assets/{source['id']}/identity-card-versions",
            headers=_headers(),
            json={
                "analysis_run_id": draft.json()["run_id"],
                "card": _identity_payload(),
                "confirmed": True,
            },
        )
        assert card_response.status_code == 201, card_response.text
        card = card_response.json()

        preview_response = _observation_preview(client, source, card)
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        observed = _observe(
            client,
            source,
            card,
            preview,
            key="observe-manual-source-locally",
        )

    assert preview["maximum_usd"] == "0.000000"
    assert preview["input_image_count"] == 0
    assert preview["model"] == "local-exact-source-match"
    assert observed.status_code == 201, observed.text
    assert observed.json()["decision"] == "pass"
    assert observed.json()["reason_codes"] == ["card_source_exact_match"]
    assert analyzer.calls == 0


def test_hidden_accessory_can_be_reviewed_but_explicit_extra_accessory_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = MutableObservationAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        card, target = _card_and_target(client, analyzer)
        analyzer.payload = _clear_payload(
            core_accessories={"value": [], "certainty": "observed"},
            visibility_limits={"value": ["clasp_hidden"], "certainty": "observed"},
        )
        preview = _observation_preview(client, target, card).json()
        observed = _observe(client, target, card, preview, key="observe-hidden")
        assert observed.status_code == 201, observed.text
        assert observed.json()["decision"] == "needs_review"
        review = client.post(
            f"/local/style-suite/api/identity-observation-runs/{observed.json()['run_id']}/reviews",
            headers=_headers(),
            json={
                "decision": "approved",
                "reason": "扣头被手腕遮挡，已人工核对原商品",
                "confirmed": True,
            },
        )
        replay = client.post(
            f"/local/style-suite/api/identity-observation-runs/{observed.json()['run_id']}/reviews",
            headers=_headers(),
            json={
                "decision": "approved",
                "reason": "扣头被手腕遮挡，已人工核对原商品",
                "confirmed": True,
            },
        )
        restored_review = client.get(
            f"/local/style-suite/api/identity-observation-runs/{observed.json()['run_id']}/review",
            headers=_headers(),
        )

        analyzer.payload = _clear_payload(
            core_accessories={
                "value": [
                    {"kind": "clasp", "visible_count": 1},
                    {"kind": "pendant", "visible_count": 1},
                ],
                "certainty": "observed",
            }
        )
        blocked = _observe(client, target, card, preview, key="observe-blocked")
        blocked_review = client.post(
            f"/local/style-suite/api/identity-observation-runs/{blocked.json()['run_id']}/reviews",
            headers=_headers(),
            json={
                "decision": "approved",
                "reason": "不能放行明确结构冲突",
                "confirmed": True,
            },
        )

    assert review.status_code == 201, review.text
    assert replay.status_code == 200, replay.text
    assert review.json()["decision"] == "approved"
    assert replay.json() == review.json() | {"created": False}
    assert restored_review.status_code == 200, restored_review.text
    assert restored_review.json() == review.json() | {"created": False}
    assert blocked.status_code == 201, blocked.text
    assert blocked.json()["decision"] == "block"
    assert blocked.json()["reason_codes"] == ["core_accessory_extra"]
    assert blocked_review.status_code == 409
    assert blocked_review.json()["error"]["code"] == "product_identity_observation_blocked"


def test_observation_errors_are_sanitized_and_cross_tenant_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = MutableObservationAnalyzer()
    secret = "s" + "k-private-observation"
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        card, target = _card_and_target(client, analyzer)
        malformed = client.post(
            f"/local/style-suite/api/assets/{target['id']}/identity-observations/preview",
            headers=_headers(),
            json={"card_version_id": card["id"], "api_key": secret},
        )
        cross_tenant = client.post(
            f"/local/style-suite/api/assets/{target['id']}/identity-observations/preview",
            headers=_headers(CUSTOMER_B),
            json={"card_version_id": card["id"]},
        )

    assert malformed.status_code == 422
    assert malformed.json() == {
        "error": {"code": "product_identity_request_invalid", "retryable": False}
    }
    assert secret not in malformed.text
    assert cross_tenant.status_code == 409
    assert cross_tenant.json()["error"]["code"] == "product_identity_observation_asset_unavailable"
    assert "/Users/" not in cross_tenant.text


def test_stale_manifest_never_dispatches_and_provider_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = MutableObservationAnalyzer()
    with _client(monkeypatch, tmp_path, analyzer=analyzer) as client:
        card, target = _card_and_target(client, analyzer)
        preview = _observation_preview(client, target, card).json()
        stale = client.post(
            f"/local/style-suite/api/assets/{target['id']}/identity-observation-runs",
            headers=_headers(),
            json={
                "card_version_id": card["id"],
                "idempotency_key": "stale-manifest",
                "confirmed_maximum_usd": preview["maximum_usd"],
                "confirmed_manifest_sha256": "0" * 64,
            },
        )
        analyzer.failure = RuntimeError(
            "provider body /Users/test-user/image.png sk-secret must not escape"
        )
        first = _observe(client, target, card, preview, key="uncertain-observation")
        replay = _observe(client, target, card, preview, key="uncertain-observation")

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "product_identity_observation_confirmation_required"
    assert first.status_code == 409
    assert replay.status_code == 409
    assert first.json()["error"]["code"] == "product_identity_observation_outcome_uncertain"
    assert replay.json() == first.json()
    assert "sk-secret" not in first.text
    assert "/Users/private" not in first.text
    assert analyzer.calls == 1


def test_disabled_observation_provider_is_stable_409(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    analyzer = MutableObservationAnalyzer()
    with _client(
        monkeypatch, tmp_path, analyzer=analyzer, enabled=False
    ) as client:
        asset = _upload_product(client)
        response = client.post(
            f"/local/style-suite/api/assets/{asset['id']}/identity-observations/preview",
            headers=_headers(),
            json={"card_version_id": str(uuid4())},
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "product_identity_observation_provider_disabled",
            "retryable": False,
        }
    }
    assert analyzer.calls == 0
