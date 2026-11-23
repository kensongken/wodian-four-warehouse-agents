"""Browser contract for the corrected one-image-to-all-suites workspace."""

import base64
import json as json_lib
import runpy
import shutil
import subprocess
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_real_fidelity_authorization import RecordingFidelityAssessor
from test_real_product_identity_authorization import identity_payload

from opc_style.api.dependencies import get_session
from opc_style.api.routes_image_jobs import get_image_generator
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import DeterministicLocalImageGenerator
from opc_style.domain.product_identity import normalize_identity_draft
from opc_style.main import create_app
from opc_style.models import (
    Base,
    Customer,
    ImageOutputJob,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductIdentityAnalysisRun,
    StyleCardSampleLink,
    StyleSample,
)
from opc_style.providers.openai_images import OpenAIImageGenerator, RealImageBudget
from opc_style.services.local_demo_sandbox import LocalDemoSandbox
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.settings import (
    LocalBridgeSettings,
    RealFidelitySettings,
    RealImageProviderSettings,
    RealProductIdentitySettings,
    StyleSuitePlanningSettings,
)

CUSTOMER_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CUSTOMER_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class _LocalIdentityAnalyzer:
    analyzer_version = "browser-product-identity-v1"
    analysis_mode = "real_product_identity_proposal"

    def analyze(self, image: Any) -> Any:
        del image
        return normalize_identity_draft(identity_payload())


class _RejectUnexpectedUploadAnalysis:
    analyzer_version = "must-not-run-during-upload"
    analysis_mode = "test_only"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, image: Any) -> Any:
        del image
        self.calls += 1
        raise AssertionError("product upload crossed the provider boundary")


@contextmanager
def _client(tmp_path: Path) -> Generator[TestClient, None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'browser.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    store = LocalSyntheticAssetStore(tmp_path / "media")
    demo_sandbox = LocalDemoSandbox(tmp_path / "demo-root")
    generator = DeterministicLocalImageGenerator()
    app = create_app()
    app.state.local_demo_sandbox = demo_sandbox
    app.state.classification_bridge_store = store
    app.state.style_suite_planning_settings = StyleSuitePlanningSettings(
        real_generation_enabled=True, candidate_estimate_usd="0.12"
    )
    app.state.real_product_identity_settings = RealProductIdentitySettings(
        enabled=True,
        api_key="test-only-identity-key",
        max_run_usd="0.10",
        _env_file=None,
    )
    app.state.real_product_identity_analyzer = _LocalIdentityAnalyzer()

    def session_override() -> Generator[Session, None, None]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_asset_content_store] = lambda: store
    app.dependency_overrides[get_image_generator] = lambda: generator
    try:
        with TestClient(app) as client:
            yield client
    finally:
        demo_sandbox.close()
        engine.dispose()


@contextmanager
def _persistent_client(
    sessions: sessionmaker[Session],
) -> Generator[TestClient, None, None]:
    """Open one app process against the configured durable local store."""
    app = create_app()
    app.state.session_factory = sessions
    app.state.style_suite_planning_settings = StyleSuitePlanningSettings(
        real_generation_enabled=True, candidate_estimate_usd="0.12"
    )
    app.state.real_product_identity_settings = RealProductIdentitySettings(
        enabled=True,
        api_key="test-only-identity-key",
        max_run_usd="0.10",
        _env_file=None,
    )
    app.state.real_product_identity_analyzer = _LocalIdentityAnalyzer()
    app.dependency_overrides[get_image_generator] = lambda: (
        DeterministicLocalImageGenerator()
    )
    with TestClient(app) as client:
        yield client


def _headers(customer_id: UUID = CUSTOMER_A) -> dict[str, str]:
    return {"X-Customer-Id": str(customer_id)}


def _run_identity_javascript(page: str, scenario: str) -> Any:
    """Execute the page's state contract with the repository's bundled Node."""
    node = shutil.which("node")
    if node is None:
        bundled = (
            Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
        )
        if bundled.exists():
            node = str(bundled)
    if node is None:
        pytest.skip("Node is unavailable for executable page contract tests")
    contract = page.split("/* identity-state-contract:start */", 1)[1].split(
        "/* identity-state-contract:end */", 1
    )[0]
    completed = subprocess.run(
        [node, "-e", f"{contract}\n{scenario}"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json_lib.loads(completed.stdout)


def _real_generator(
    provider_calls: list[httpx.Request], *, budget: str = "3.00"
) -> OpenAIImageGenerator:
    image = BytesIO()
    Image.new("RGB", (24, 24), (30, 60, 90)).save(image, format="JPEG")
    encoded = base64.b64encode(image.getvalue()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"b64_json": encoded}] * 3},
        )

    return OpenAIImageGenerator(
        RealImageProviderSettings(
            enabled=True,
            api_key="test-only-key",
            max_run_usd=Decimal(budget),
            _env_file=None,
        ),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(budget)),
    )


def _failing_real_generator(
    provider_calls: list[httpx.Request],
) -> OpenAIImageGenerator:
    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(500, json={"error": {"message": "retry"}})

    return OpenAIImageGenerator(
        RealImageProviderSettings(
            enabled=True,
            api_key="test-only-key",
            max_run_usd=Decimal("3.00"),
            _env_file=None,
        ),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("3.00")),
    )


def _uploaded_product(client: TestClient) -> dict[str, Any]:
    token = uuid4().hex
    image = BytesIO()
    Image.new("RGB", (24, 24), (20, 80, 120)).save(image, format="PNG")
    uploaded = client.post(
        "/local/style-suite/api/product",
        headers={
            **_headers(),
            "X-Product-Id": f"real-pilot-product-{token}",
            "X-Photo-Id": f"real-pilot-photo-{token}",
            "Content-Type": "image/png",
        },
        content=image.getvalue(),
    )
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()


def _image_bytes(image_format: str, *, size: tuple[int, int] = (24, 24)) -> bytes:
    image = BytesIO()
    Image.new("RGB", size, (20, 80, 120)).save(image, format=image_format)
    return image.getvalue()


def _product_upload_side_effects(tmp_path: Path) -> tuple[int, int, int, int]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'browser.db'}")
    try:
        with Session(engine) as session:
            return (
                len(list(session.scalars(select(Customer.id)))),
                len(list(session.scalars(select(ProductAsset.id)))),
                len(list(session.scalars(select(ProductAssetPermissionEvent.id)))),
                len(list(session.scalars(select(ProductIdentityAnalysisRun.id)))),
            )
    finally:
        engine.dispose()


def _stored_media_files(tmp_path: Path) -> list[Path]:
    media_root = tmp_path / "media"
    return sorted(
        path.relative_to(media_root) for path in media_root.rglob("*") if path.is_file()
    )


def _raised_local_bridge_settings() -> LocalBridgeSettings:
    """Bypass settings validation to exercise the upload's hard fuse."""
    return LocalBridgeSettings.model_construct(
        local_classification_bridge_max_bytes=20 * 1024 * 1024,
        local_classification_bridge_max_width=8192,
        local_classification_bridge_max_height=8192,
        local_classification_bridge_max_pixels=64_000_000,
    )


def _confirmed_identity_card_id(client: TestClient, asset_id: str) -> str:
    preview = client.get(
        f"/local/style-suite/api/assets/{asset_id}/identity-analysis-preview",
        headers=_headers(),
    )
    assert preview.status_code == 200, preview.text
    analysis = client.post(
        f"/local/style-suite/api/assets/{asset_id}/identity-analysis-runs",
        headers=_headers(),
        json={
            "idempotency_key": f"browser-identity-{uuid4().hex}",
            "confirmed_maximum_usd": preview.json()["maximum_usd"],
            "confirmed_manifest_sha256": preview.json()["manifest_sha256"],
        },
    )
    assert analysis.status_code == 201, analysis.text
    confirmed = client.post(
        f"/local/style-suite/api/assets/{asset_id}/identity-card-versions",
        headers=_headers(),
        json={
            "analysis_run_id": analysis.json()["run_id"],
            "confirmed": True,
            "card": identity_payload(),
        },
    )
    assert confirmed.status_code == 201, confirmed.text
    return str(confirmed.json()["id"])


def _confirmed_identity_observation_scope(
    client: TestClient, asset_id: str, card_id: str
) -> dict[str, str | None]:
    preview = client.post(
        f"/local/style-suite/api/assets/{asset_id}/identity-observations/preview",
        headers=_headers(),
        json={"card_version_id": card_id},
    )
    assert preview.status_code == 200, preview.text
    observed = client.post(
        f"/local/style-suite/api/assets/{asset_id}/identity-observation-runs",
        headers=_headers(),
        json={
            "card_version_id": card_id,
            "idempotency_key": f"browser-observation-{uuid4().hex}",
            "confirmed_maximum_usd": preview.json()["maximum_usd"],
            "confirmed_manifest_sha256": preview.json()["manifest_sha256"],
        },
    )
    assert observed.status_code == 201, observed.text
    review_id: str | None = None
    if observed.json()["decision"] == "needs_review":
        review = client.post(
            "/local/style-suite/api/identity-observation-runs/"
            f"{observed.json()['run_id']}/reviews",
            headers=_headers(),
            json={
                "decision": "approved",
                "reason": "测试中已人工核对同一商品",
                "confirmed": True,
            },
        )
        assert review.status_code == 201, review.text
        review_id = review.json()["review_id"]
    return {
        "product_identity_observation_run_id": observed.json()["run_id"],
        "product_identity_observation_review_id": review_id,
    }


def _create_real_style_matrix(client: TestClient) -> None:
    """Create two customer styles with five total slots in the real runtime."""
    evidence_ids: list[str] = []
    for index, color in enumerate(((210, 80, 30), (40, 120, 210)), start=1):
        stream = BytesIO()
        Image.new("RGB", (24, 24), color).save(stream, format="JPEG")
        content = stream.getvalue()
        uploaded = client.post(
            "/v1/style-samples/content",
            headers={
                **_headers(),
                "X-Photo-Id": f"matrix-style-{uuid4().hex}-{index}",
                "X-Content-Sha256": sha256(content).hexdigest(),
                "X-Mime-Type": "image/jpeg",
                "X-Consent-Granted": "true",
                "X-Consent-Provenance": "local-explicit-style-upload",
                "X-Consent-Text-Version": "local-style-upload-v1",
            },
            content=content,
        )
        assert uploaded.status_code == 201, uploaded.text
        evidence_ids.append(uploaded.json()["id"])
    confirmed = client.post(
        "/v1/style-cards/scenes/confirm",
        headers=_headers(),
        json={
            "confirmation_key": f"real-style-matrix-{uuid4().hex}",
            "confirmed": True,
            "groups": [
                {
                    "name": "真实暖光桌面",
                    "evidence_sample_ids": [evidence_ids[0]],
                    "slots": [
                        {
                            "slot_key": slot_key,
                            "label": label,
                            "rules": {
                                "aspect_ratio": "4:5",
                                "shot_distance": "close",
                                "camera_angle": "front",
                            },
                        }
                        for slot_key, label in (
                            ("hero", "主图"),
                            ("detail", "细节"),
                            ("wear", "佩戴"),
                        )
                    ],
                },
                {
                    "name": "真实清透白底",
                    "evidence_sample_ids": [evidence_ids[1]],
                    "slots": [
                        {
                            "slot_key": slot_key,
                            "label": label,
                            "rules": {
                                "aspect_ratio": "4:5",
                                "shot_distance": "close",
                                "camera_angle": "front",
                            },
                        }
                        for slot_key, label in (("hero", "主图"), ("side", "侧面"))
                    ],
                },
            ],
        },
    )
    assert confirmed.status_code == 201, confirmed.text


def _prepared_demo_pilot(client: TestClient) -> dict[str, Any]:
    asset = _uploaded_product(client)
    demo = client.post(
        "/local/style-suite/api/demo/runs",
        headers=_headers(),
        json={"idempotency_key": f"demo-pilot-scope-{uuid4().hex}"},
    )
    assert demo.status_code == 201, demo.text
    selected = demo.json()["items"][0]
    card_id = _confirmed_identity_card_id(client, asset["id"])
    observation = _confirmed_identity_observation_scope(client, asset["id"], card_id)
    return {
        "product_id": asset["product_id"],
        "source_asset_id": asset["id"],
        "product_identity_card_version_id": card_id,
        **observation,
        "scene_style_version_id": selected["scene_style_version_id"],
        "slot_key": selected["slot_key"],
        "idempotency_key": "real-pilot-browser-001",
    }


def _prepared_pilot(client: TestClient) -> dict[str, Any]:
    evidence_ids: list[str] = []
    colors = tuple(
        (40 + index * 12, 55 + index * 7, 75 + index * 5) for index in range(10)
    )
    for index, color in enumerate(colors, start=1):
        stream = BytesIO()
        Image.new("RGB", (24, 24 + index), color).save(stream, format="JPEG")
        content = stream.getvalue()
        uploaded = client.post(
            "/v1/style-samples/content",
            headers={
                **_headers(),
                "X-Photo-Id": f"paid-style-{uuid4().hex}-{index}",
                "X-Content-Sha256": sha256(content).hexdigest(),
                "X-Mime-Type": "image/jpeg",
                "X-Consent-Granted": "true",
                "X-Consent-Provenance": "local-explicit-style-upload",
                "X-Consent-Text-Version": "local-style-upload-v1",
            },
            content=content,
        )
        assert uploaded.status_code == 201, uploaded.text
        evidence_ids.append(uploaded.json()["id"])
    confirmed = client.post(
        "/v1/style-cards/scenes/confirm",
        headers=_headers(),
        json={
            "confirmation_key": str(uuid4()),
            "confirmed": True,
            "groups": [
                {
                    "name": f"真实风格-{uuid4().hex[:8]}",
                    "evidence_sample_ids": evidence_ids,
                    "analysis_mode": "real_visual_analysis_proposal",
                    "analyzer_version": "browser-real-analysis-v1",
                    "proposal_fingerprint": "f" * 64,
                    "palette": ["低饱和冷灰"],
                    "lighting": ["柔和左侧漫射光"],
                    "composition": ["主体居中并保留上方留白"],
                    "backgrounds": ["浅灰无缝背景"],
                    "field_evidence": {
                        "palette": evidence_ids,
                        "lighting": evidence_ids,
                        "composition": evidence_ids,
                        "backgrounds": evidence_ids,
                    },
                    "slots": [
                        {
                            "slot_key": "hero",
                            "label": "主图",
                            "rules": {
                                "aspect_ratio": "4:5",
                                "shot_distance": "close",
                                "camera_angle": "front",
                                "composition": ["centered"],
                                "background": ["light gray seamless"],
                                "lighting": ["soft left diffuse"],
                            },
                        }
                    ],
                }
            ],
        },
    )
    assert confirmed.status_code == 201, confirmed.text
    style = confirmed.json()[0]
    asset = _uploaded_product(client)
    card_id = _confirmed_identity_card_id(client, asset["id"])
    observation = _confirmed_identity_observation_scope(client, asset["id"], card_id)
    return {
        "product_id": asset["product_id"],
        "source_asset_id": asset["id"],
        "product_identity_card_version_id": card_id,
        **observation,
        "scene_style_version_id": style["version_id"],
        "slot_key": style["slots"][0]["slot_key"],
        "idempotency_key": f"real-pilot-browser-{uuid4().hex}",
    }


def test_real_fidelity_routes_quote_then_execute_once(tmp_path: Path) -> None:
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        scope = _prepared_pilot(client)
        generated = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json={**scope, "confirmed_max_usd": "3.00"},
        )
        assert generated.status_code == 201, generated.text
        assessor = RecordingFidelityAssessor()
        client.app.state.real_fidelity_settings = RealFidelitySettings(
            enabled=True,
            api_key="test-secret",
            max_run_usd="0.50",
            _env_file=None,
        )
        client.app.state.real_fidelity_assessor = assessor
        run_id = generated.json()["id"]

        quote = client.get(
            f"/local/style-suite/api/runs/{run_id}/fidelity-preview",
            headers=_headers(),
        )
        assert quote.status_code == 200, quote.text
        assert quote.json()["image_count"] == 4
        assert quote.json()["attempt_state"] == "not_started"
        denied = client.post(
            f"/local/style-suite/api/runs/{run_id}/fidelity-checks",
            headers=_headers(),
            json={
                "idempotency_key": "route-fidelity-round-1",
                "confirmed_maximum_usd": "0",
                "confirmed_manifest_sha256": quote.json()["input_manifest_sha256"],
            },
        )
        assert denied.status_code == 409
        assert assessor.calls == 0
        accepted = client.post(
            f"/local/style-suite/api/runs/{run_id}/fidelity-checks",
            headers=_headers(),
            json={
                "idempotency_key": "route-fidelity-round-1",
                "confirmed_maximum_usd": quote.json()["maximum_usd"],
                "confirmed_manifest_sha256": quote.json()["input_manifest_sha256"],
            },
        )
        replay = client.post(
            f"/local/style-suite/api/runs/{run_id}/fidelity-checks",
            headers=_headers(),
            json={
                "idempotency_key": "route-fidelity-round-1",
                "confirmed_maximum_usd": quote.json()["maximum_usd"],
                "confirmed_manifest_sha256": quote.json()["input_manifest_sha256"],
            },
        )
        restored_quote = client.get(
            f"/local/style-suite/api/runs/{run_id}/fidelity-preview",
            headers=_headers(),
        )
        state = client.get(f"/local/style-suite/api/runs/{run_id}", headers=_headers())
        assert state.status_code == 200, state.text
        cards = state.json()["review_cards"]
        assert [card["fidelity_summary_zh"] for card in cards] == [
            "商品类别发生变化",
            "商品身份一致",
            "暂时无法确认",
        ]
        assert restored_quote.status_code == 200, restored_quote.text
        assert restored_quote.json()["attempt_state"] == "provider_responded"
        assert len({card["source_preview_url"] for card in cards}) == 1
        source = client.get(cards[0]["source_preview_url"], headers=_headers())
        assert source.status_code == 200
        assert source.headers["cache-control"] == "private, no-store"
        assert Image.open(BytesIO(source.content)).format in {"JPEG", "PNG"}
        assert (
            client.get(
                cards[0]["source_preview_url"], headers=_headers(CUSTOMER_B)
            ).status_code
            == 404
        )
        forged_red = client.post(
            f"/v1/image-outputs/{cards[0]['job_id']}/candidates/"
            f"{cards[0]['candidate_id']}/review",
            headers=_headers(),
            json={
                "decision": "approved",
                "reason": None,
                "idempotency_key": "forged-red-as-green",
                "fidelity_state": "green",
                "fidelity_flags": [],
            },
        )
        assert forged_red.status_code == 409
        assert forged_red.json()["error"]["code"] == "candidate_fidelity_rejected"
        unknown = client.post(
            f"/v1/image-outputs/{cards[2]['job_id']}/candidates/"
            f"{cards[2]['candidate_id']}/review",
            headers=_headers(),
            json={
                "decision": "approved",
                "reason": None,
                "idempotency_key": "unknown-cannot-approve",
            },
        )
        assert unknown.status_code == 409
        assert unknown.json()["error"]["code"] == "candidate_fidelity_unavailable"
        green = client.post(
            f"/v1/image-outputs/{cards[1]['job_id']}/candidates/"
            f"{cards[1]['candidate_id']}/review",
            headers=_headers(),
            json={
                "decision": "approved",
                "reason": None,
                "idempotency_key": "server-green-human-selection",
            },
        )
        assert green.status_code == 201, green.text
        assert green.json()["publishable"] is False

    assert accepted.status_code == 201, accepted.text
    assert replay.status_code == 200, replay.text
    assert assessor.calls == 1
    assert len(provider_calls) == 1


def test_latest_real_run_for_product_supersedes_stale_browser_pointer(
    tmp_path: Path,
) -> None:
    """A newer durable run for the same product must win over stale local state."""
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        scope = _prepared_pilot(client)
        first = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json={
                **scope,
                "idempotency_key": "latest-run-old",
                "confirmed_max_usd": "3.00",
            },
        )
        second = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json={
                **scope,
                "idempotency_key": "latest-run-new",
                "confirmed_max_usd": "3.00",
            },
        )

        latest = client.get(
            f"/local/style-suite/api/assets/{scope['source_asset_id']}/latest-run",
            headers=_headers(),
        )
        cross_tenant = client.get(
            f"/local/style-suite/api/assets/{scope['source_asset_id']}/latest-run",
            headers=_headers(CUSTOMER_B),
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert latest.status_code == 200, latest.text
    assert latest.json()["run_id"] == second.json()["id"]
    assert latest.json()["run_id"] != first.json()["id"]
    assert latest.json()["created_at"]
    assert cross_tenant.status_code == 404
    assert len(provider_calls) == 2


def test_latest_real_run_for_customer_recovers_after_browser_scope_was_lost(
    tmp_path: Path,
) -> None:
    """Durable latest run must recover even if an old page erased local storage."""
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        scope = _prepared_pilot(client)
        first = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json={
                **scope,
                "idempotency_key": "customer-latest-old",
                "confirmed_max_usd": "3.00",
            },
        )
        second = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json={
                **scope,
                "idempotency_key": "customer-latest-new",
                "confirmed_max_usd": "3.00",
            },
        )

        latest = client.get("/local/style-suite/api/latest-run", headers=_headers())
        cross_tenant = client.get(
            "/local/style-suite/api/latest-run", headers=_headers(CUSTOMER_B)
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert latest.status_code == 200, latest.text
    assert latest.json()["run_id"] == second.json()["id"]
    assert cross_tenant.status_code == 404
    assert len(provider_calls) == 2


def test_page_prefers_server_latest_run_over_stale_saved_run(tmp_path: Path) -> None:
    """Browser recovery must choose the server's newest run for the visible product."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    result = _run_identity_javascript(
        page,
        """
console.log(JSON.stringify({
  replaced:preferredRestoredRunId('old-run',{run_id:'new-run'}),
  fallback:preferredRestoredRunId('old-run',null),
  serverOnly:preferredRestoredRunId(null,{run_id:'new-run'}),
  customerRecovery:preferredRestoredRunId(null,null,{run_id:'customer-new-run'})
}));
""",
    )

    assert result == {
        "replaced": "new-run",
        "fallback": "old-run",
        "serverOnly": "new-run",
        "customerRecovery": "customer-new-run",
    }


def test_page_labels_the_current_restored_batch_before_paid_review(
    tmp_path: Path,
) -> None:
    """The operator must see which exact batch a paid check would inspect."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    result = _run_identity_javascript(
        page,
        """
console.log(JSON.stringify(runContextView({
  run_id:'12345678-abcd-4abc-8abc-123456789abc',
  created_at:'2026-09-26T16:38:11Z'
})));
""",
    )

    assert result["hidden"] is False
    assert "当前批次：12345678" in result["textContent"]
    assert "生成时间：" in result["textContent"]
    assert "最新可用批次" in result["textContent"]
    assert 'id="run-context"' in page


def test_successful_restored_batch_allows_a_fresh_separately_confirmed_generation(
    tmp_path: Path,
) -> None:
    """A settled batch must not permanently lock later paid style generations."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    result = _run_identity_javascript(
        page,
        """
console.log(JSON.stringify({
  settled:pilotRunGate(
    {progress:{needs_reconciliation:0}},
    {state:'awaiting_review',items:[{state:'succeeded',failure_reason:null}]}
  ),
  uncertain:pilotRunGate(
    {progress:{needs_reconciliation:1}},
    {state:'needs_reconciliation',items:[{state:'needs_reconciliation',failure_reason:null}]}
  ),
  failed:pilotRunGate(
    {progress:{needs_reconciliation:0}},
    {state:'failed',items:[{state:'failed',failure_reason:'provider_http_500'}]}
  )
}));
""",
    )

    assert result == {
        "settled": {"blocked": False, "errorCode": None},
        "uncertain": {"blocked": True, "errorCode": "needs_reconciliation"},
        "failed": {"blocked": True, "errorCode": "provider_http_500"},
    }


def test_local_workspace_is_not_mounted_in_production(monkeypatch: Any) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    with TestClient(create_app()) as client:
        assert client.get("/local/style-suite").status_code == 404


def test_page_exposes_two_workspaces_without_sensitive_or_publish_controls(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        response = client.get("/local/style-suite")
        assert response.status_code == 200
        page = response.text
        for label in (
            "我的真实风格",
            "风格样板",
            "套图位置",
            "一张图生成全部套图",
            "选择一张新商品图",
            "首轮生成",
            "生成全部套图",
            "商品身份不一致，不能选择",
            "人工确认并选为最终图",
            "本地测试空间",
            "学习历史风格",
            "上传新商品",
            "审核整套图片",
            "拖拽或点击选择商品图片",
            "加载示例风格",
            "示例风格（仅演示）",
            "不会出现在真实付费生成列表",
            "上传自己的历史照片",
            "新商品图请在右侧上传",
            "上传历史风格照片",
            "分析我的拍摄风格",
            "确认这些风格",
            "检测到演示照片与真实照片混用，请重新分析真实照片。",
            "我确认这些照片可用于学习拍摄风格",
            "戒指补样",
            "独立吊坠补样",
            "补样步骤：上传自有单件商品图",
            "达到 3 张只代表可开始本地试运行",
        ):
            assert label in page
        for visual_contract in (
            'class="hero"',
            'class="steps"',
            'class="upload-zone"',
            'class="mode-badge"',
            'class="workspace-card',
            "@media(max-width:820px)",
            "--champagne:",
            'aria-live="polite"',
            'class="upstream-link"',
            'id="history-files"',
            'id="consent"',
            'id="analyze"',
            'id="confirm-styles"',
            "historyStatus=document.querySelector('#history-status')",
            'id="product-preview"',
            'id="product-status"',
            "requestedBenchmarkCustomer||localStorage.getItem('style-suite-customer-id')||crypto.randomUUID()",
            "if(!requestedBenchmarkCustomer)localStorage.setItem('style-suite-customer-id',customer)",
            "product-${crypto.randomUUID()}",
            "上传失败：",
            "async function normalizeStyleImage(file)",
            "canvas.toBlob",
            "Math.min(1,3000/Math.max(bitmap.width,bitmap.height))",
            "`history-${digest}`",
            "localStorage.setItem('style-suite-customer-id',customer)",
            "localStorage.setItem('style-suite-run-id',run.id)",
            "清理未使用的商品图",
            "`照片 ${f.name} 处理失败：${e.message}`",
            'id="ring-readiness"',
            'id="pendant-readiness"',
            "<option>pendant</option>",
            "sample.has_gold_mask",
            "机器草稿 ${stats.drafts} 张",
            "AI 初校 ${stats.corrected} 张",
            "AI 初校候选已保存",
            "AI 初校待人审",
            "filter==='ai_corrected'&&x.has_gold_mask&&!x.human_reviewed",
            "复查 AI 初校候选",
            "${stats.pending} 张待适用性确认",
        ):
            assert visual_contract in page
        assert "<option>pendant</option>" in page
        assert 'value="pendant">独立吊坠</option>' in page
        assert "pendant:'独立吊坠'" in page
        assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" not in page
        for forbidden in ("API key", "系统提示词", "发布到生产", "storage_path"):
            assert forbidden not in page
        for hidden_numbering in (
            "风格 ${i+1}",
            "· #${c.sequence_number}",
        ):
            assert hidden_numbering not in page


def test_page_renders_bounded_batch_scope_cost_and_recoverable_matrix(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    assert 'id="batch-style-grid"' in page
    assert 'id="batch-cost-confirmation"' in page
    assert 'id="batch-result-matrix"' in page
    assert "本次生成范围" in page
    assert "本次付费范围" not in page
    assert "本次费用 USD 0，不调用付费模型" in page
    assert 'id="pilot-style"' not in page
    assert 'id="pilot-slot"' not in page
    assert "/local/style-suite/api/batch-runs/preview" in page
    assert "/local/style-suite/api/batch-runs" in page


def test_page_exposes_refresh_safe_pixel_preserving_review_gates(
    tmp_path: Path,
) -> None:
    """The browser must stop at both human-review gates before local composition."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        'id="foreground-review"',
        'id="foreground-engine-copy"',
        'id="foreground-original"',
        'id="foreground-cutout"',
        'id="foreground-mask"',
        'id="foreground-disagreement"',
        'id="prepare-foreground-disagreement"',
        "coarse_only_detached_dominant",
        "更像背景多选候选",
        "优先检查珠子、细绳、扣头",
        'id="foreground-shadow"',
        'id="foreground-shadow-verdict"',
        'id="review-foreground-shadow"',
        "本机双模型影子检查",
        "技术上可通过",
        "需要人工检查",
        "已阻断",
        'id="foreground-confirmation"',
        'id="confirm-foreground"',
        'id="reject-foreground"',
        "我确认商品完整，且没有混入背景物体。",
        "/local/style-suite/api/products/${currentAsset.id}/foreground-preview",
        "本机基础抠图没有识别出完整商品，已安全停止",
        "图片未外发、未扣费，也不会进入生成",
        "/local/style-suite/api/products/${currentAsset.id}/foreground-versions",
        "async function loadForegroundState()",
        "requestedReviewAsset=localReviewParams.get('review_asset_id')",
        "/local/style-suite/api/assets/${requestedReviewAsset}",
        "待人工复核的商品图",
        "if(requestedReviewAsset){await beginManualIdentity();return}",
        "document.body.classList.add('direct-review')",
        ".direct-review .history-card",
        "if(foregroundPreview)renderForegroundVersion(foregroundPreview)",
        "红色：仅初筛模型选中 · 黄色：仅精修模型选中",
        "/disagreement/prepare",
        "现在可以继续确认商品抠图",
        "foreground_candidate_busy",
        "本机抠图模型正在处理另一张图",
        "function renderForegroundShadow(version)",
        "function productUploadScale(width,height)",
        "async function prepareProductUpload(file)",
        "Math.sqrt(16000000/(width*height))",
        "原文件未修改",
        "version.quality_flags.includes('touches_edge')",
        "shadowReview.onclick=openMaskEditor",
        'id="pixel-background-review"',
        'id="pixel-background-image"',
        'id="confirm-pixel-background"',
        'id="reject-pixel-background"',
        'id="pixel-composition-editor"',
        'id="pixel-composition-preview"',
        'id="refresh-pixel-composition"',
        'id="save-pixel-composition"',
        "我确认背景中没有首饰、人物、文字或标志",
        "/local/style-suite/api/pixel-backgrounds/preview",
        "/local/style-suite/api/pixel-background-runs",
        "/decision",
        "/compose",
        "/compose-preview",
        "/compose-recommendation",
        "pixelCompositionSaveAllowed",
        "台面可用空间不足",
        "pixel_background_tabletop_insufficient",
        "pixel_composition_category_calibration_required",
        "当前商品品类尚无真实落位校准证据",
        "function pixelCompositionPayload(state)",
        "function pixelCompositionEditorPayload()",
        "async function renderPixelCompositionPreview()",
        "正在本机计算商品落位建议",
        "output_width:outputWidth",
        "output_height:outputHeight",
        "保持原背景比例",
        "像素保真合成",
        "生成式候选",
        "模型 ${pixelBackgroundPreview.model}",
        "发送 ${pixelBackgroundPreview.provider_image_count} 张风格参考图",
        "最高费用 USD ${pixelBackgroundPreview.maximum_usd}",
        "publishable=false",
    ):
        assert required in page

    assert "foregroundContentPath" not in page
    assert "maskContentPath" not in page
    assert "access_token" not in page
    assert "SAM 2.1 + BiRefNet 本地管线只在本机" not in page

    observation_renderer = page.split("function renderIdentityObservation()", 1)[
        1
    ].split("async function quoteIdentityObservation()", 1)[0]
    assert (
        "if(identityObservationReady()&&!foregroundPreview&&!foregroundBusy)"
        "void loadForegroundState()"
    ) in observation_renderer

    foreground_loader = page.split("async function loadForegroundState()", 1)[1].split(
        "async function decideForeground", 1
    )[0]
    for required_guard in (
        "if(!asset||foregroundBusy)return",
        "const generation=productSelectionState.generation,currentAsset=asset",
        "generation!==productSelectionState.generation",
        "asset.id!==currentAsset.id",
    ):
        assert required_guard in foreground_loader
    assert "confirmedIdentityCard" not in foreground_loader

    restore_flow = page.split("async function restoreBrowserState()", 1)[1].split(
        "await restoreBrowserState()", 1
    )[0]
    assert "await loadForegroundState()" in restore_flow

    foreground_renderer = page.split("function renderForegroundVersion(version)", 1)[
        1
    ].split("async function loadForegroundState", 1)[0]
    for required_guard in (
        "const imageLoadToken=++foregroundImageLoadToken",
        "const imageScope=()=>imageLoadToken===foregroundImageLoadToken",
        "loadPrivateImage(foregroundOriginal",
        "imageScope",
        "Promise.all([",
        "confirmedForeground=accepted?version:undefined",
        "抠图证据图加载失败，已禁止确认",
        "version.pipeline_manifest_sha256",
        "仅用于流程测试，不代表双模型质量验收",
        "identityMissing=!confirmedIdentityCard",
        "可先检查或修正遮罩，确认按钮暂不开放",
    ):
        assert required_guard in foreground_renderer

    foreground_review_helper = page.split(
        "function foregroundReviewMessage(version)", 1
    )[1].split("function renderForegroundVersion(version)", 1)[0]
    assert "subject_too_small_for_primary_image" in foreground_review_helper
    assert "建议重拍近景" in foreground_review_helper

    foreground_reset = page.split("function resetForegroundState()", 1)[1].split(
        "function resetIdentityObservationState", 1
    )[0]
    for image_name in (
        "foregroundOriginal",
        "foregroundCutout",
        "foregroundMask",
    ):
        assert f"clearPrivateImage({image_name})" in foreground_reset
    assert "function clearPrivateImage(image)" in page

    batch_quote = page.split("async function quoteSelectedBatch()", 1)[1].split(
        "function onBatchScopeChange", 1
    )[0]
    assert "pixel_preserving_mask_required" in batch_quote
    assert "请先确认商品抠图" in batch_quote
    assert "batchCostConfirmation.disabled=missingForeground" in batch_quote


def test_browser_copy_comes_from_actual_extractor_evidence(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    renderer = page.split("function renderForegroundVersion(version)", 1)[1].split(
        "async function loadForegroundState", 1
    )[0]
    assert "version.pipeline_manifest_sha256" in renderer
    assert "version.extractor_version" in renderer
    assert "已封存 SAM 2.1 + BiRefNet 双模型候选" in renderer
    assert "本机基线；仅用于流程测试" in renderer


def test_page_exposes_a_separate_zero_cost_visual_demo_workspace(
    tmp_path: Path,
) -> None:
    """The demo action must render visible images without entering paid controls."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        'id="demo-workspace"',
        'id="demo-style-cards"',
        'id="run-demo"',
        'id="demo-cards"',
        "免费演示区",
        "用示例商品生成 3 张演示图",
        "本地演示图 · 不连接外部模型 · 不收费 · 不可发布",
        "/local/style-suite/api/demo/bootstrap",
        "/local/style-suite/api/demo/runs",
        "外部请求 0 次",
    ):
        assert required in page
    assert "createDemoProduct" not in page
    assert "/local/style-suite/api/demo-pilot/runs" not in page
    assert "demoRunKey=`opcStyleDemoRunV1:${customerId}`" in page
    assert "renderDemoStyles(x.styles)" in page
    assert "${style.name}" in page
    assert "${style.slots.map(slot=>slot.label).join(' · ')}" in page
    assert "暖光桌面" not in page
    assert "清透白底" not in page

    demo_run_function = page.split("async function runDemoWorkspace()", 1)[1].split(
        "prepare.onclick", 1
    )[0]
    assert "body:JSON.stringify({idempotency_key:'browser-demo-v1'})" in (
        demo_run_function
    )
    for forbidden_input in ("product_id", "source_asset_id", "confirmed_max_usd"):
        assert forbidden_input not in demo_run_function

    restore_contract = (
        "const savedDemoRunId=localStorage.getItem(demoRunKey);"
        "if(savedDemoRunId){json('/local/style-suite/api/demo/runs/'"
        "+savedDemoRunId).then(async state=>{await loadDemoWorkspace();"
        "renderDemoRun(state)}).catch(error=>{if(error.code==='http_404')"
        "localStorage.removeItem(demoRunKey)})}"
    )
    assert restore_contract in page


def test_page_disambiguates_duplicate_style_names_before_confirmation(
    tmp_path: Path,
) -> None:
    """Provider name collisions must not turn the confirm button into a 422 trap."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    assert "function uniqueProposalNames(groups)" in page
    assert "const displayNames=uniqueProposalNames(groups)" in page
    assert 'value="${displayNames[i]}"' in page
    assert "风格名称不能重复" in page
    assert "真实风格已保存" in page
    assert "clearPilotError();" in page


def test_page_exposes_one_click_identity_locked_review_and_refresh(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite/identity-locked").text

    for required in (
        'id="identity-generate"',
        "查看生成范围",
        "/v1/image-outputs/product-style/identity-locked/preview",
        "X-Confirmed-Maximum-Usd",
        "X-Confirmed-Manifest-Sha256",
        "商品原图不发送给背景模型",
        "不是供应商账单保证",
        "保留区像素校验通过",
        "仍需人工审核",
        'id="mask-original"',
        'id="mask-composite"',
        "恢复误删",
        "identity_locked_style_samples_insufficient",
        "查看真实原图",
        "查看场景图",
        "/v1/image-outputs/product-style/identity-locked",
        "localStorage.setItem('identity-locked-job-id',job.id)",
        "const savedIdentityJobId=localStorage.getItem('identity-locked-job-id')",
    ):
        assert required in page


def test_browser_harness_binds_demo_scope_and_real_media_manifest(
    tmp_path: Path,
) -> None:
    """Acceptance must prove demo scope and real-media non-mutation before paid work."""
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_style_suite_browser_acceptance.py"
    )
    namespace = runpy.run_path(str(script_path))
    media_manifest = namespace["_media_manifest"]
    script = script_path.read_text(encoding="utf-8")

    media_root = tmp_path / "real-media"
    empty = media_manifest(media_root)
    media_root.mkdir()
    (media_root / "candidate.jpg").write_bytes(b"one")
    first = media_manifest(media_root)
    (media_root / "candidate.jpg").write_bytes(b"two")
    second = media_manifest(media_root)

    assert empty["file_count"] == 0
    assert first["file_count"] == 1
    assert first["sha256"] != second["sha256"]
    for required_contract in (
        '"LOCAL_STYLE_ASSET_ROOT": str(local_asset_root)',
        '"real_media_manifest_unchanged_after_demo"',
        '"real_media_manifest_before_demo"',
        '"real_media_manifest_after_demo"',
        '"demo_selected_style_name"',
        '"demo_selected_slot_key"',
    ):
        assert required_contract in script


def test_page_points_pending_real_styles_to_the_exact_confirmation_action(
    tmp_path: Path,
) -> None:
    """A pending proposal must not look like an unresponsive preview button."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    assert "真实风格已分析但尚未确认：请点击左侧“确认这些风格”。" in page


def test_page_confirms_each_style_with_only_its_group_evidence(
    tmp_path: Path,
) -> None:
    """Different learned styles must not silently reuse the whole photo library."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    assert "evidence_sample_ids:g.evidence_sample_ids" in page
    assert "styleProposal.library_sample_ids||g.evidence_sample_ids" not in page


def test_page_preserves_reviewed_camera_and_prop_rules_when_confirming_style(
    tmp_path: Path,
) -> None:
    """Confirmed slots must retain the analyzed setup instead of forcing front view."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    assert "square:'1:1',portrait:'4:5',landscape:'3:2'" in page
    assert "camera_angle:(observations.angle||[])[0]||'front'" in page
    assert "shot_distance:(observations.crop||[])[0]||'close'" in page
    assert "props:observations.props||[]" in page


def test_paid_pilot_controls_require_bounded_batch_and_exact_cost_confirmation(
    tmp_path: Path,
) -> None:
    """The browser must make the bounded paid scope visible before execution."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        'id="batch-style-grid"',
        'id="batch-cost-confirmation"',
        'id="batch-result-matrix"',
        "最多 3 个风格",
        "最高费用 USD ${quoted.maximum_estimated_cost_usd}",
        "/local/style-suite/api/batch-runs",
        "真实模型：GPT-Image-2 · 不自动发布",
    ):
        assert required in page
    assert "publishable=false" in page
    assert 'name="model"' not in page
    assert 'name="prompt"' not in page


def test_page_explains_every_disabled_generation_gate_and_identity_action(
    tmp_path: Path,
) -> None:
    """A disabled paid action must explain the next human step in Chinese."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        'id="identity-panel"',
        'id="identity-analysis-confirmation"',
        'id="run-identity-analysis"',
        'id="start-manual-identity"',
        'id="identity-draft"',
        'id="confirm-identity-card"',
        'id="pilot-readiness"',
        "身份卡分析费用",
        "确认商品身份",
        "本次可能已计费，系统不会自动重试",
        "可改用本地人工填写继续，不再发送图片",
        "可选付费模型提取，也可直接改用本地人工填写",
        "startManualIdentity.hidden=false",
        "请先上传一张新商品图",
        "先提取并确认商品身份",
        "请选择至少一个真实风格和套图位置",
        "请确认本次准确生图费用",
        "let identityPreview,identityProposal,confirmedIdentityCard",
        "identityBusy=false",
        "async function loadIdentityState()",
        "async function previewIdentityAnalysis()",
        "async function runIdentityAnalysis()",
        "async function beginManualIdentity()",
        "function renderIdentityDraft()",
        "async function confirmIdentityCard()",
        "function updatePilotEnabled()",
        "product_identity_card_version_id:confirmedIdentityCard.id",
        "/identity-manual-drafts",
        "人工填写可见特征（本地免费）",
        "不发送图片 · 不收费",
        "画面可见限制（手或托盘出现不等于遮挡）",
        "真实遮挡（商品像素被盖住）",
    ):
        assert required in page


def test_identity_page_contract_scopes_restore_and_uncertain_retry_to_current_asset(
    tmp_path: Path,
) -> None:
    """Changing products must not carry proposal, card, or retry authority forward."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    load_identity = page.split("async function loadIdentityState()", 1)[1].split(
        "async function previewIdentityAnalysis()", 1
    )[0]
    product_change = page.split("product.onchange=async()=>", 1)[1].split(
        "preview.onclick", 1
    )[0]
    analysis = page.split("async function runIdentityAnalysis()", 1)[1].split(
        "function renderIdentityDraft()", 1
    )[0]

    assert "/identity-card-versions" in load_identity
    assert "versions[0]" in load_identity
    assert "confirmedIdentityCard" in load_identity
    assert "identityProposal" not in load_identity
    assert "resetIdentityState()" in product_change
    assert "await loadIdentityState()" in product_change
    assert "await loadForegroundState()" in product_change
    assert "productUploadController.abort()" in product_change
    assert "generation!==productSelectionState.generation" in product_change
    assert "localStorage.removeItem('style-suite-run-id')" in product_change
    assert "identityBusy=true" in analysis
    assert "runIdentityAnalysisButton.hidden=true" in analysis
    assert "localStorage.setItem(identityUncertainKey()" in analysis


def test_product_restore_keeps_scope_during_transient_outage_and_clears_dead_asset(
    tmp_path: Path,
) -> None:
    """A temporary service outage must not erase the pointer needed for recovery."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    result = _run_identity_javascript(
        page,
        """
        console.log(JSON.stringify({
          unavailable: productRestoreFailureAction(httpStatusError(503).code),
          network: productRestoreFailureAction(undefined),
          missing: productRestoreFailureAction(httpStatusError(404).code),
          unavailableAsset: productRestoreFailureAction('product_identity_asset_unavailable'),
          changed: productRestoreFailureAction('product_identity_manifest_changed')
        }));
        """,
    )

    assert result["unavailable"] == {
        "clearScope": False,
        "message": "服务暂时不可用，已保留商品记录；恢复后刷新页面即可继续。",
    }
    assert result["network"] == result["unavailable"]
    assert result["missing"] == {
        "clearScope": True,
        "message": "当前商品记录已失效，请重新选择商品图。",
    }
    assert result["unavailableAsset"] == result["missing"]
    assert result["changed"] == result["missing"]


def test_identity_draft_blocks_claims_that_are_not_visible_product_facts(
    tmp_path: Path,
) -> None:
    """Prohibited material, authenticity, origin, and price claims stay disabled."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    assert "function identityTextAllowed(value)" in page
    assert "!identityTextAllowed(value)" in page
    for prohibited_example in (
        "材质",
        "正品",
        "产地",
        "价格",
        "ownership",
    ):
        assert prohibited_example in page


def test_identity_selection_state_ignores_late_upload_and_binds_consent_to_b(
    tmp_path: Path,
) -> None:
    """A late A response cannot replace the visible, paid-consent B scope."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    result = _run_identity_javascript(
        page,
        """
let state=createProductSelectionState();
state=reduceProductSelection(state,{type:'begin',fileName:'A.png'});
const generationA=state.generation;
state=reduceProductSelection(state,{type:'begin',fileName:'B.png'});
const generationB=state.generation;
state=reduceProductSelection(state,{type:'uploaded',generation:generationB,asset:{id:'asset-b',product_id:'product-b',content_sha256:'b'.repeat(64)}});
state=reduceProductSelection(state,{type:'identity_preview',generation:generationB,assetId:'asset-b',preview:{asset_id:'asset-b',source_content_sha256:'b'.repeat(64),manifest_sha256:'manifest-b'}});
const beforeLate=JSON.stringify(state);
state=reduceProductSelection(state,{type:'uploaded',generation:generationA,asset:{id:'asset-a',product_id:'product-a',content_sha256:'a'.repeat(64)}});
console.log(JSON.stringify({unchanged:beforeLate===JSON.stringify(state),fileName:state.visibleFileName,assetId:state.asset.id,consent:state.identityConsent}));
""",
    )

    assert result == {
        "unchanged": True,
        "fileName": "B.png",
        "assetId": "asset-b",
        "consent": {"assetId": "asset-b", "manifestSha256": "manifest-b"},
    }


def test_every_late_a_async_completion_is_inert_after_b_becomes_visible(
    tmp_path: Path,
) -> None:
    """All five post-await completion types must remain scoped to visible B."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    result = _run_identity_javascript(
        page,
        """
(async()=>{
  const assetA={id:'asset-a',product_id:'product-a',content_sha256:'a'.repeat(64)};
  const assetB={id:'asset-b',product_id:'product-b',content_sha256:'b'.repeat(64)};
  function visibleB(){
    let state=createProductSelectionState();
    state=reduceProductSelection(state,{type:'begin',fileName:'A.png'});
    const generationA=state.generation;
    state=reduceProductSelection(state,{type:'uploaded',generation:generationA,asset:assetA});
    state=reduceProductSelection(state,{type:'begin',fileName:'B.png'});
    state=reduceProductSelection(state,{type:'uploaded',generation:state.generation,asset:assetB});
    return {state,generationA};
  }
  async function finishLate(action){
    const setup=visibleB(),before=setup.state;
    await Promise.resolve();
    const after=reduceProductSelection(before,{...action,generation:setup.generationA,assetId:'asset-a'});
    return {sameObject:after===before,assetId:after.asset.id,visible:after.visibleFileName,uiState:after.uiState??null,preview:after.identityPreview??null,consent:after.identityConsent??null,proposal:after.identityProposal??null,card:after.confirmedCard??null,plan:after.plan??null,run:after.run??null};
  }
  const stale={
    identityPreview:await finishLate({type:'identity_preview',preview:{asset_id:'asset-a',source_content_sha256:'a'.repeat(64),manifest_sha256:'manifest-a'}}),
    identityAnalysis:await finishLate({type:'identity_analysis',proposal:{run_id:'analysis-a'}}),
    identityConfirm:await finishLate({type:'identity_confirm',card:{id:'card-a'}}),
    pilotPreview:await finishLate({type:'pilot_preview',plan:{id:'plan-a'}}),
    pilotCreate:await finishLate({type:'pilot_create',run:{id:'run-a'}})
  };
  let current=visibleB().state;
  current=reduceProductSelection(current,{type:'identity_preview',generation:current.generation,assetId:'asset-b',preview:{asset_id:'asset-b',source_content_sha256:'b'.repeat(64),manifest_sha256:'manifest-b'}});
  current=reduceProductSelection(current,{type:'identity_confirm',generation:current.generation,assetId:'asset-b',card:{id:'card-b'}});
  const canDispatch={
    identityA:canDispatchIdentityAnalysis(current,'asset-a','manifest-a'),
    identityB:canDispatchIdentityAnalysis(current,'asset-b','manifest-b'),
    pilotA:typeof canDispatchPilotCreate==='function'?canDispatchPilotCreate(current,'asset-a','card-a'):false,
    pilotB:typeof canDispatchPilotCreate==='function'?canDispatchPilotCreate(current,'asset-b','card-b'):false
  };
  console.log(JSON.stringify({stale,canDispatch}));
})();
""",
    )

    inert = {
        "sameObject": True,
        "assetId": "asset-b",
        "visible": "B.png",
        "uiState": "product_uploaded",
        "preview": None,
        "consent": None,
        "proposal": None,
        "card": None,
        "plan": None,
        "run": None,
    }
    assert result["stale"] == {
        "identityPreview": inert,
        "identityAnalysis": inert,
        "identityConfirm": inert,
        "pilotPreview": inert,
        "pilotCreate": inert,
    }
    assert result["canDispatch"] == {
        "identityA": False,
        "identityB": True,
        "pilotA": False,
        "pilotB": True,
    }


def test_current_b_async_completions_update_only_b_scoped_state(tmp_path: Path) -> None:
    """The same state transitions accept current B results, preventing dead guards."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    result = _run_identity_javascript(
        page,
        """
(async()=>{
  let state=createProductSelectionState();
  state=reduceProductSelection(state,{type:'begin',fileName:'B.png'});
  const generation=state.generation,asset={id:'asset-b',product_id:'product-b',content_sha256:'b'.repeat(64)};
  state=reduceProductSelection(state,{type:'uploaded',generation,asset});
  for(const action of [
    {type:'identity_preview',preview:{asset_id:'asset-b',source_content_sha256:'b'.repeat(64),manifest_sha256:'manifest-b'}},
    {type:'identity_analysis',proposal:{run_id:'analysis-b'}},
    {type:'identity_confirm',card:{id:'card-b'}},
    {type:'pilot_preview',plan:{id:'plan-b'}},
    {type:'pilot_create',run:{id:'run-b'}}
  ]){await Promise.resolve();state=reduceProductSelection(state,{...action,generation,assetId:'asset-b'});}
  console.log(JSON.stringify({assetId:state.asset.id,preview:state.identityPreview?.manifest_sha256??null,proposal:state.identityProposal?.run_id??null,card:state.confirmedCard?.id??null,plan:state.plan?.id??null,run:state.run?.id??null,uiState:state.uiState??null}));
})();
""",
    )

    assert result == {
        "assetId": "asset-b",
        "preview": "manifest-b",
        "proposal": "analysis-b",
        "card": "card-b",
        "plan": "plan-b",
        "run": "run-b",
        "uiState": "pilot_created",
    }


def test_identity_restore_prefers_current_product_scope_over_stale_run(
    tmp_path: Path,
) -> None:
    """A saved run is useful only when it describes the current product bytes."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    result = _run_identity_javascript(
        page,
        """
const product={id:'asset-b',product_id:'product-b',content_sha256:'b'.repeat(64)};
const stale={id:'asset-a',product_id:'product-a',content_sha256:'a'.repeat(64),state:'failed'};
const matching={...product,state:'pending'};
console.log(JSON.stringify({stale:preferredRestoredScope(product,stale),matchingPending:preferredRestoredScope(product,matching),runOnlyFailed:preferredRestoredScope(null,stale),uncertain:identityReconciliationRequired({reconciliation_required:true},false)}));
""",
    )

    assert result == {
        "stale": "product",
        "matchingPending": "run",
        "runOnlyFailed": "run",
        "uncertain": True,
    }


@pytest.mark.parametrize(
    ("value", "allowed"),
    [
        ("主体珠大小接近", True),
        ("golden ratio visible", True),
        ("18K金配件", False),
        ("属于客户", False),
        ("USD 9", False),
    ],
)
def test_identity_browser_validator_matches_domain_for_representative_claims(
    tmp_path: Path, value: str, allowed: bool
) -> None:
    """The page and domain must reject the same visible-claim counterexamples."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    browser_allowed = _run_identity_javascript(
        page,
        f"console.log(JSON.stringify(identityTextAllowed({json_lib.dumps(value)})));",
    )
    try:
        normalize_identity_draft(
            identity_payload(
                relative_proportions={"value": value, "certainty": "observed"}
            )
        )
        domain_allowed = True
    except DomainRuleError:
        domain_allowed = False

    assert browser_allowed is allowed
    assert browser_allowed is domain_allowed


def test_identity_editor_is_beside_original_and_controls_are_named(
    tmp_path: Path,
) -> None:
    """The real draft is in the two-column layout and dynamic inputs are named."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    layout = page.split('<div class="identity-layout">', 1)[1].split(
        '<div class="identity-actions">', 1
    )[0]
    assert 'id="product-preview"' in layout
    assert 'id="identity-draft"' in layout
    for contract in (
        "certainty.ariaLabel=",
        "select.ariaLabel=",
        "input.ariaLabel=",
        "count.ariaLabel=",
        "textarea.ariaLabel=",
        "error.setAttribute('role','alert')",
        "control.setAttribute('aria-invalid'",
    ):
        assert contract in page
    names = _run_identity_javascript(
        page,
        "console.log(JSON.stringify(identityControlAccessibleNames('核心配件','扣头')));",
    )
    assert names == {
        "certainty": "核心配件识别状态",
        "value": "核心配件",
        "count": "扣头可见数量",
    }
    group_state = _run_identity_javascript(
        page,
        """
const state=typeof identityFieldAccessibility==='function'?identityFieldAccessibility('primary_colors',{kind:'multi'},'主体颜色请选择 1–4 项'):null;
console.log(JSON.stringify(state));
""",
    )
    assert group_state == {
        "invalid": True,
        "fieldsetAriaInvalid": "true",
        "describedBy": "identity-error-primary_colors",
    }


def test_current_product_original_is_restorable_without_exposing_storage_path(
    tmp_path: Path,
) -> None:
    """Authenticated asset bytes restore the original without leaking a path."""
    with _client(tmp_path) as client:
        asset = _uploaded_product(client)
        own = client.get(
            f"/local/style-suite/api/assets/{asset['id']}/content",
            headers=_headers(),
        )
        foreign = client.get(
            f"/local/style-suite/api/assets/{asset['id']}/content",
            headers=_headers(CUSTOMER_B),
        )

    assert own.status_code == 200
    assert own.headers["content-type"] == "image/png"
    assert own.headers["cache-control"] == "private, no-store"
    assert own.headers["x-content-type-options"] == "nosniff"
    with Image.open(BytesIO(own.content)) as image:
        assert image.size == (24, 24)
    assert foreign.status_code == 404
    assert str(tmp_path) not in foreign.text


def test_current_product_metadata_can_open_a_direct_review_link(
    tmp_path: Path,
) -> None:
    """A customer-scoped asset id restores review state without re-uploading."""
    with _client(tmp_path) as client:
        asset = _uploaded_product(client)
        own = client.get(
            f"/local/style-suite/api/assets/{asset['id']}", headers=_headers()
        )
        foreign = client.get(
            f"/local/style-suite/api/assets/{asset['id']}",
            headers=_headers(CUSTOMER_B),
        )

    assert own.status_code == 200
    assert own.json()["id"] == asset["id"]
    assert own.json()["content_sha256"] == asset["content_sha256"]
    assert foreign.status_code == 404


@pytest.mark.parametrize(
    ("image_format", "claimed_type", "expected_type"),
    [
        ("JPEG", "image/png", "image/jpeg"),
        ("PNG", "image/jpeg", "image/png"),
    ],
)
def test_product_upload_uses_actual_jpeg_or_png_magic_for_private_content(
    tmp_path: Path, image_format: str, claimed_type: str, expected_type: str
) -> None:
    """Persisted MIME follows decoded bytes, never the caller's claimed header."""
    with _client(tmp_path) as client:
        token = uuid4().hex
        uploaded = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": f"magic-product-{token}",
                "X-Photo-Id": f"magic-photo-{token}",
                "Content-Type": claimed_type,
            },
            content=_image_bytes(image_format),
        )
        assert uploaded.status_code == 201, uploaded.text
        asset = uploaded.json()
        content = client.get(
            f"/local/style-suite/api/assets/{asset['id']}/content",
            headers=_headers(),
        )

    assert asset["mime_type"] == expected_type
    assert content.status_code == 200
    assert content.headers["content-type"] == expected_type
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    with Image.open(BytesIO(content.content)) as decoded:
        assert decoded.format == image_format


def test_product_upload_rejects_webp_bytes_before_persistence(tmp_path: Path) -> None:
    """A decodable non-contract format cannot be mislabeled and stored as JPEG."""
    media_root = tmp_path / "media"
    with _client(tmp_path) as client:
        before = sorted(path.relative_to(media_root) for path in media_root.rglob("*"))
        rejected = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "webp-product",
                "X-Photo-Id": "webp-photo",
                "Content-Type": "image/jpeg",
            },
            content=_image_bytes("WEBP"),
        )
        after = sorted(path.relative_to(media_root) for path in media_root.rglob("*"))

    assert rejected.status_code == 422
    assert rejected.json() == {
        "error": {"code": "product_image_format_unsupported", "retryable": False}
    }
    assert before == after


def test_product_upload_rejects_more_than_ten_mib_before_persistence(
    tmp_path: Path,
) -> None:
    analyzer = _RejectUnexpectedUploadAnalysis()
    with _client(tmp_path) as client:
        client.app.state.real_product_identity_analyzer = analyzer
        before_files = _stored_media_files(tmp_path)
        rejected = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "oversized-byte-product",
                "X-Photo-Id": "oversized-byte-photo",
                "Content-Type": "image/png",
            },
            content=b"x" * (10 * 1024 * 1024 + 1),
        )

    assert rejected.status_code == 413
    assert rejected.json() == {
        "error": {"code": "product_image_limits_exceeded", "retryable": False}
    }
    assert _product_upload_side_effects(tmp_path) == (0, 0, 0, 0)
    assert _stored_media_files(tmp_path) == before_files
    assert analyzer.calls == 0


def test_local_bridge_environment_cannot_raise_product_upload_hard_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_MAX_BYTES", str(20 * 1024 * 1024))
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_MAX_WIDTH", "8192")
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_MAX_HEIGHT", "8192")
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_MAX_PIXELS", "64000000")

    with pytest.raises(ValidationError):
        LocalBridgeSettings(_env_file=None)


def test_product_upload_hard_byte_limit_survives_bypassed_raised_settings(
    tmp_path: Path,
) -> None:
    analyzer = _RejectUnexpectedUploadAnalysis()
    with _client(tmp_path) as client:
        client.app.state.local_bridge_settings = _raised_local_bridge_settings()
        client.app.state.real_product_identity_analyzer = analyzer
        before_files = _stored_media_files(tmp_path)
        rejected = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "hard-byte-limit-product",
                "X-Photo-Id": "hard-byte-limit-photo",
                "Content-Type": "image/png",
            },
            content=b"x" * 10_485_761,
        )

    assert rejected.status_code == 413
    assert rejected.json() == {
        "error": {"code": "product_image_limits_exceeded", "retryable": False}
    }
    assert _product_upload_side_effects(tmp_path) == (0, 0, 0, 0)
    assert _stored_media_files(tmp_path) == before_files
    assert analyzer.calls == 0


def test_product_upload_hard_pixel_limits_survive_bypassed_raised_settings(
    tmp_path: Path,
) -> None:
    oversized = BytesIO()
    Image.new("1", (5000, 5000), 1).save(oversized, format="PNG")
    analyzer = _RejectUnexpectedUploadAnalysis()
    with _client(tmp_path) as client:
        client.app.state.local_bridge_settings = _raised_local_bridge_settings()
        client.app.state.real_product_identity_analyzer = analyzer
        before_files = _stored_media_files(tmp_path)
        rejected = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "hard-pixel-limit-product",
                "X-Photo-Id": "hard-pixel-limit-photo",
                "Content-Type": "image/png",
            },
            content=oversized.getvalue(),
        )

    assert rejected.status_code == 413
    assert rejected.json() == {
        "error": {"code": "product_image_limits_exceeded", "retryable": False}
    }
    assert _product_upload_side_effects(tmp_path) == (0, 0, 0, 0)
    assert _stored_media_files(tmp_path) == before_files
    assert analyzer.calls == 0


def test_product_upload_honors_lower_custom_limits(tmp_path: Path) -> None:
    limited = LocalBridgeSettings(
        local_classification_bridge_max_width=32,
        local_classification_bridge_max_height=32,
        local_classification_bridge_max_pixels=1024,
    )
    analyzer = _RejectUnexpectedUploadAnalysis()
    with _client(tmp_path) as client:
        client.app.state.local_bridge_settings = limited
        client.app.state.real_product_identity_analyzer = analyzer
        before_files = _stored_media_files(tmp_path)
        rejected = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "lower-limit-product",
                "X-Photo-Id": "lower-limit-photo",
                "Content-Type": "image/png",
            },
            content=_image_bytes("PNG", size=(33, 31)),
        )

    assert rejected.status_code == 413
    assert rejected.json() == {
        "error": {"code": "product_image_limits_exceeded", "retryable": False}
    }
    assert _product_upload_side_effects(tmp_path) == (0, 0, 0, 0)
    assert _stored_media_files(tmp_path) == before_files
    assert analyzer.calls == 0


def test_product_upload_rejects_4097_square_image_before_persistence(
    tmp_path: Path,
) -> None:
    oversized = BytesIO()
    Image.new("1", (4097, 4097), 1).save(oversized, format="PNG")
    analyzer = _RejectUnexpectedUploadAnalysis()
    with _client(tmp_path) as client:
        client.app.state.real_product_identity_analyzer = analyzer
        before_files = _stored_media_files(tmp_path)
        rejected = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "oversized-dimension-product",
                "X-Photo-Id": "oversized-dimension-photo",
                "Content-Type": "image/png",
            },
            content=oversized.getvalue(),
        )

    assert rejected.status_code == 413
    assert rejected.json() == {
        "error": {"code": "product_image_limits_exceeded", "retryable": False}
    }
    assert _product_upload_side_effects(tmp_path) == (0, 0, 0, 0)
    assert _stored_media_files(tmp_path) == before_files
    assert analyzer.calls == 0


def test_product_upload_rejects_multiframe_png_before_persistence(
    tmp_path: Path,
) -> None:
    animated = BytesIO()
    first = Image.new("RGB", (16, 16), (20, 80, 120))
    second = Image.new("RGB", (16, 16), (120, 80, 20))
    first.save(animated, format="PNG", save_all=True, append_images=[second])
    analyzer = _RejectUnexpectedUploadAnalysis()
    with _client(tmp_path) as client:
        client.app.state.real_product_identity_analyzer = analyzer
        before_files = _stored_media_files(tmp_path)
        rejected = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "animated-product",
                "X-Photo-Id": "animated-photo",
                "Content-Type": "image/png",
            },
            content=animated.getvalue(),
        )

    assert rejected.status_code == 413
    assert rejected.json() == {
        "error": {"code": "product_image_limits_exceeded", "retryable": False}
    }
    assert _product_upload_side_effects(tmp_path) == (0, 0, 0, 0)
    assert _stored_media_files(tmp_path) == before_files
    assert analyzer.calls == 0


@pytest.mark.parametrize(
    "bomb",
    [Image.DecompressionBombWarning, Image.DecompressionBombError],
)
def test_product_upload_fails_closed_on_decompression_bomb_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bomb: type[Warning] | type[BaseException],
) -> None:
    from opc_style.api import routes_local_style_suite

    original_open = routes_local_style_suite.Image.open

    def bomb_open(*args: Any, **kwargs: Any) -> Any:
        if bomb is Image.DecompressionBombWarning:
            warnings.warn("test decompression bomb", bomb, stacklevel=2)
            return original_open(*args, **kwargs)
        raise bomb("test decompression bomb")

    monkeypatch.setattr(routes_local_style_suite.Image, "open", bomb_open)
    analyzer = _RejectUnexpectedUploadAnalysis()
    with _client(tmp_path) as client:
        client.app.state.real_product_identity_analyzer = analyzer
        before_files = _stored_media_files(tmp_path)
        rejected = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "decompression-bomb-product",
                "X-Photo-Id": "decompression-bomb-photo",
                "Content-Type": "image/png",
            },
            content=_image_bytes("PNG"),
        )

    assert rejected.status_code == 422
    assert rejected.json() == {
        "error": {"code": "product_image_invalid", "retryable": False}
    }
    assert _product_upload_side_effects(tmp_path) == (0, 0, 0, 0)
    assert _stored_media_files(tmp_path) == before_files
    assert analyzer.calls == 0


def test_run_projection_exposes_only_chinese_confirmed_identity_summary(
    tmp_path: Path,
) -> None:
    """The browser projection needs a useful card label, never provider internals."""
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        scope = _prepared_pilot(client)
        generated = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json={**scope, "confirmed_max_usd": "3.00"},
        )
        assert generated.status_code == 201, generated.text
        state = client.get(
            f"/local/style-suite/api/runs/{generated.json()['id']}",
            headers=_headers(),
        )

    assert state.status_code == 200, state.text
    payload = state.json()
    assert payload["identity_card_version"] == 1
    assert payload["identity_card_summary_zh"] == (
        "手链 · 绿色、金色调 · 圆珠 · 可见组件 18 个"
    )
    serialized = state.text.lower()
    for forbidden in (
        "storage_path",
        "provider_payload",
        "api_key",
        "authorization",
        "relative_proportions",
        "distinguishing_marks",
    ):
        assert forbidden not in serialized
    assert len(provider_calls) == 1


def test_paid_preview_contains_only_server_approved_customer_styles(
    tmp_path: Path,
) -> None:
    """The browser selector must consume a server-filtered real-style list."""
    with _client(tmp_path) as client:
        demo = client.post(
            "/local/style-suite/api/synthetic-history", headers=_headers()
        )
        assert demo.status_code == 201, demo.text
        prepared = _prepared_pilot(client)
        preview = client.post(
            "/v1/style-suites/preview",
            headers=_headers(),
            json={
                "product_id": prepared["product_id"],
                "source_asset_id": prepared["source_asset_id"],
            },
        )

    assert preview.status_code == 200, preview.text
    items = preview.json()["items"]
    assert {item["scene_style_version_id"] for item in items} == {
        prepared["scene_style_version_id"]
    }
    assert {item["style_snapshot"]["style_origin"] for item in items} == {
        "customer_upload"
    }


def test_history_upload_offers_batch_and_folder_collection_controls(
    tmp_path: Path,
) -> None:
    """Removing either explicit input would break a supported intake path."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        'id="history-files"',
        'id="history-folder"',
        "批量选择图片",
        "选择整个文件夹",
        "webkitdirectory",
        'id="history-selection"',
        "继续追加",
        "移除",
    ):
        assert required in page


def test_page_recovers_saved_library_status_after_refresh(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _prepared_pilot(client)
        status = client.get("/local/style-suite/api/style-library", headers=_headers())
        page = client.get("/local/style-suite").text

    assert status.status_code == 200, status.text
    assert status.json()["active_sample_count"] == 10
    assert status.json()["available_sample_count"] == 10
    assert status.json()["analysis_ready"] is True
    assert "/local/style-suite/api/style-library" in page
    assert "本地已恢复" in page
    assert "分析已保存的历史照片" in page


def test_detected_style_names_are_localized_for_confirmation_and_selection(
    tmp_path: Path,
) -> None:
    """Raw analyzer labels must not leak back into the Chinese workflow."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for chinese_name in (
        "蓝调",
        "红调",
        "绿调",
        "方形构图",
        "竖幅构图",
        "横幅构图",
    ):
        assert chinese_name in page
    assert "localizeStyleName(group.suggested_name)" in page
    assert 'value="${displayNames[i]}"' in page
    assert "localizeStyleName(items[0].style_name)" in page


def test_paid_pilot_notice_switches_from_synthetic_to_truthful_real_mode(
    tmp_path: Path,
) -> None:
    """A paid route cannot retain the synthetic no-network/no-cost notice."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        'id="mode-notice"',
        "真实模型：GPT-Image-2 · 可能连接外部模型 · 最高费用 USD 3.00 · 不自动发布 · 仍需勾选确认",
        "本地测试空间 · 只有勾选明确授权后才会调用已配置模型 · 费用以上一步报价为准 · 所有结果不可发布",
        "当前没有已确认的生图任务 · 不会自动产生费用 · 所有结果不可发布",
    ):
        assert required in page


def test_page_requires_separate_fidelity_quote_and_renders_server_owned_states(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        'id="fidelity-gate"',
        'id="fidelity-confirmation"',
        'id="run-fidelity"',
        "/fidelity-preview",
        "/fidelity-checks",
        "confirmed_manifest_sha256",
        "fidelityPreview.attempt_state",
        "本批次已有保真检查记录，为避免重复扣费已禁止再次提交",
        'class="source-comparison"',
        "商品身份不一致，不能选择",
        "暂时无法确认是同一商品，不能选择",
        "存在轻微偏差，确认接受后才能选择",
        "机器检查未发现商品身份变化，仍需人工确认",
    ):
        assert required in page
    assert "fidelity_state:good" not in page
    assert "fidelity_flags:good" not in page
    assert "suite.items.length===1" not in page


def test_page_injects_real_runtime_without_rendering_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser can show provider state but must never receive the API key."""
    secret = "test-secret-must-not-render"
    monkeypatch.setenv("REAL_IMAGE_ENABLED", "true")
    monkeypatch.setenv("REAL_IMAGE_API_KEY", secret)
    monkeypatch.setenv("REAL_IMAGE_MODEL", "gpt-image-2")
    monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "0.36")

    with _client(tmp_path) as client:
        page = client.get("/local/style-suite/identity-locked").text

    assert (
        'const identityRuntime={"real_enabled":true,"model":"gpt-image-2",'
        '"budget_usd":"0.36"};'
    ) in page
    assert secret not in page


def test_paid_pilot_errors_explain_safe_retry_or_paid_state_uncertainty(
    tmp_path: Path,
) -> None:
    """The visible UI must translate stable API codes into safe next actions."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        'id="pilot-error"',
        "style_suite_pilot_scope_unavailable",
        "所选风格或位置已失效，请重新选择后再预览。",
        "style_reference_limit_exceeded",
        "历史风格参考图已重新整理，请重新预览。",
        "needs_reconciliation",
        "付费状态不确定，请勿重试；请联系工作人员并核对供应商账单。",
        "function showPilotError(error)",
    ):
        assert required in page


def test_paid_pilot_refresh_blocks_actual_failed_provider_runs_until_safe_recovery(
    tmp_path: Path,
) -> None:
    """A 201 failed run is still a paid boundary and must lock the UI."""
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    for required in (
        "let pilotBlocked=false,pilotBusy=false,pilotRecoveryRequired=false",
        "failedItem.failure_reason",
        "供应商未能生成候选图；本次付费处理请勿重试；请联系工作人员并核对供应商账单。",
        "pilotBlocked||pilotBusy",
        "pilotRecoveryRequired",
    ):
        assert required in page


def test_local_workspace_accepts_real_history_pixels_and_confirms_proposals(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        demo = client.post(
            "/local/style-suite/api/synthetic-history", headers=_headers()
        )
        assert demo.status_code == 201, demo.text
        evidence_ids: list[str] = []
        for index, color in enumerate(((220, 30, 30), (30, 60, 220)), start=1):
            stream = BytesIO()
            Image.new("RGB", (24, 24 + index), color).save(stream, format="JPEG")
            content = stream.getvalue()
            uploaded = client.post(
                "/v1/style-samples/content",
                headers={
                    **_headers(),
                    "X-Photo-Id": f"history-upload-{index}",
                    "X-Content-Sha256": sha256(content).hexdigest(),
                    "X-Mime-Type": "image/jpeg",
                    "X-Consent-Granted": "true",
                    "X-Consent-Provenance": "local-explicit-style-upload",
                    "X-Consent-Text-Version": "local-style-upload-v1",
                },
                content=content,
            )
            assert uploaded.status_code == 201, uploaded.text
            evidence_ids.append(uploaded.json()["id"])

        proposal = client.post("/v1/style-cards/main/propose", headers=_headers())
        assert proposal.status_code == 200, proposal.text
        body = proposal.json()
        assert body["authoritative"] is False
        assert body["analysis_mode"] == "synthetic_test_only"
        assert sorted(
            sample_id
            for group in body["groups"]
            for sample_id in group["evidence_sample_ids"]
        ) == sorted(evidence_ids)

        groups = []
        for group in body["groups"]:
            groups.append(
                {
                    "name": group["suggested_name"],
                    "proposal_keys": [group["proposal_key"]],
                    "evidence_sample_ids": group["evidence_sample_ids"],
                    "slots": [
                        {
                            "slot_key": slot,
                            "label": slot,
                            "rules": {
                                "aspect_ratio": "4:5",
                                "shot_distance": "close",
                                "camera_angle": "front",
                            },
                        }
                        for slot in group["suggested_slots"]
                    ],
                }
            )
        confirmed = client.post(
            "/v1/style-cards/scenes/confirm",
            headers=_headers(),
            json={
                "confirmation_key": "browser-real-history-v1",
                "confirmed": True,
                "groups": groups,
            },
        )
        assert confirmed.status_code == 201, confirmed.text
        assert len(confirmed.json()) == len(body["groups"])


def test_browser_never_confirms_synthetic_analysis_as_real_style(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        page = client.get("/local/style-suite").text

    assert "本地测试分析不能作为真实生图风格" in page
    assert "proposal_fingerprint" in page
    assert "square:'1:1',portrait:'4:5',landscape:'3:2'" in page
    assert "field_evidence" in page
    assert "风格证据" in page
    assert "风格尚未完成真实学习" in page


def test_real_local_api_drives_complete_matrix_progress_and_review_cards(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        prepared = client.post(
            "/local/style-suite/api/synthetic-history", headers=_headers()
        )
        assert prepared.status_code == 201
        assert prepared.json()["sample_count"] == 10
        assert prepared.json()["style_count"] == 2
        assert prepared.json()["slot_count"] == 5

        uploaded = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "new-product-001",
                "X-Photo-Id": "new-product-photo-001",
                "Content-Type": "image/png",
            },
            content=_image_bytes("PNG"),
        )
        assert uploaded.status_code == 201, uploaded.text
        asset = uploaded.json()

        preview = client.post(
            "/v1/style-suites/preview",
            headers=_headers(),
            json={"product_id": asset["product_id"], "source_asset_id": asset["id"]},
        )
        assert preview.status_code == 409, preview.text
        assert preview.json()["error"]["code"] == "style_suite_no_active_styles"

        _create_real_style_matrix(client)

        executed = client.post(
            "/local/style-suite/api/runs",
            headers=_headers(),
            json={
                "product_id": asset["product_id"],
                "source_asset_id": asset["id"],
                "idempotency_key": "browser-suite-001",
            },
        )
        assert executed.status_code == 201, executed.text
        run = executed.json()
        assert run["candidate_count"] == 15, run
        assert run["publishable"] is False

        state = client.get(
            f"/local/style-suite/api/runs/{run['id']}", headers=_headers()
        )
        assert state.status_code == 200, state.text
        body = state.json()
        assert body["workspaces"] == ["historical_styles", "generate_suite"]
        assert body["generation_adapter"] == "deterministic-local-image-v1"
        assert body["generation_mode"] == "synthetic_local_deterministic"
        assert body["outbound_attempts"] == 0
        assert len(body["review_cards"]) == 15
        assert all("fidelity_state" in card for card in body["review_cards"])
        assert all("fidelity_summary_zh" in card for card in body["review_cards"])
        assert all(
            card["source_preview_url"].startswith(
                f"/local/style-suite/api/runs/{run['id']}/source/content"
            )
            for card in body["review_cards"]
        )
        assert all(
            card["preview_url"].startswith(
                f"/local/style-suite/api/runs/{run['id']}/candidates/"
            )
            for card in body["review_cards"]
        )
        assert body["progress"]["needs_reconciliation"] == 0
        assert body["publishable"] is False

        image_response = client.get(
            body["review_cards"][0]["preview_url"], headers=_headers()
        )
        assert image_response.status_code == 200
        assert image_response.headers["content-type"] == "image/jpeg"
        assert Image.open(BytesIO(image_response.content)).format == "JPEG"
        assert (
            client.get(
                body["review_cards"][0]["preview_url"], headers=_headers(CUSTOMER_B)
            ).status_code
            == 404
        )
        assert (
            client.get(
                body["review_cards"][0]["preview_url"].split("?", 1)[0]
            ).status_code
            == 404
        )
        assert client.get(
            f"/local/style-suite/api/runs/{run['id']}/candidates/../../etc/passwd",
            headers=_headers(),
        ).status_code in {404, 422}

        first = body["review_cards"][0]
        rejected = client.post(
            f"/v1/image-outputs/{first['job_id']}/candidates/{first['candidate_id']}/review",
            headers=_headers(),
            json={
                "decision": "product_detail_inaccurate",
                "reason": "不像同一商品",
                "idempotency_key": "browser-red-001",
                "fidelity_state": "red",
                "fidelity_flags": ["product_form_changed"],
            },
        )
        assert rejected.status_code == 201, rejected.text
        reviewed = client.get(
            f"/local/style-suite/api/runs/{run['id']}", headers=_headers()
        ).json()
        assert reviewed["review_cards"][0]["fidelity_state"] == "red"
        assert reviewed["review_cards"][0]["fidelity_flags"] == ["product_form_changed"]
        assert reviewed["review_cards"][0]["candidate_state"] == "rejected"

        replay = client.post(
            "/local/style-suite/api/runs",
            headers=_headers(),
            json={
                "product_id": asset["product_id"],
                "source_asset_id": asset["id"],
                "idempotency_key": "browser-suite-001",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["candidate_count"] == 15

        denied = client.get(
            f"/local/style-suite/api/runs/{run['id']}", headers=_headers(CUSTOMER_B)
        )
        assert denied.status_code == 404


def test_zero_cost_demo_pilot_generates_exactly_three_local_candidates(
    tmp_path: Path,
) -> None:
    """The visible demo uses one demo style/slot and never calls a real provider."""
    with _client(tmp_path) as client:
        executed = client.post(
            "/local/style-suite/api/demo/runs",
            headers=_headers(),
            json={"idempotency_key": "visible-zero-cost-demo-001"},
        )

        assert executed.status_code == 201, executed.text
        run = executed.json()
        assert run["style_count"] == 1
        assert run["slot_count"] == 1
        assert run["candidate_count"] == 3
        assert run["publishable"] is False
        assert run["items"][0]["style_snapshot"]["style_origin"] == "local_demo"
        assert run["items"][0]["style_name"] == "暖光桌面"
        assert run["items"][0]["slot_key"] == "hero"

        state = client.get(
            f"/local/style-suite/api/demo/runs/{run['id']}", headers=_headers()
        )
        assert state.status_code == 200, state.text
        body = state.json()
        assert len(body["review_cards"]) == 3
        assert body["generation_mode"] == "synthetic_local_deterministic"
        assert body["outbound_attempts"] == 0
        assert {
            (card["style_name"], card["slot_key"]) for card in body["review_cards"]
        } == {("暖光桌面", "hero")}

        candidate = client.get(body["review_cards"][0]["preview_url"])
        assert candidate.status_code == 200, candidate.text
        assert candidate.headers["x-synthetic-artifact"] == "true"


def test_demo_history_stays_separate_when_real_styles_already_exist(
    tmp_path: Path,
) -> None:
    """Real customer evidence must never inflate or replace the demo library."""
    with _client(tmp_path) as client:
        _prepared_pilot(client)

        prepared = client.post(
            "/local/style-suite/api/synthetic-history", headers=_headers()
        )

        assert prepared.status_code == 201, prepared.text
        assert prepared.json() == {
            "sample_count": 10,
            "style_count": 2,
            "slot_count": 5,
            "analysis_adapter": "customer_confirmed_synthetic_history",
            "publishable": False,
        }


def test_uploaded_assets_candidates_and_review_survive_app_restart(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A browser restart must not erase customer inputs or completed work."""
    asset_root = (tmp_path / "durable-assets").resolve()
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(asset_root))
    engine = create_database_engine(f"sqlite:///{tmp_path / 'restart.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    run_id = ""

    try:
        with _persistent_client(sessions) as client:
            prepared = client.post(
                "/local/style-suite/api/synthetic-history", headers=_headers()
            )
            assert prepared.status_code == 201, prepared.text
            _prepared_pilot(client)

            image = BytesIO()
            Image.new("RGB", (24, 24), (20, 80, 120)).save(image, format="PNG")
            uploaded = client.post(
                "/local/style-suite/api/product",
                headers={
                    **_headers(),
                    "X-Product-Id": "restart-product",
                    "X-Photo-Id": "restart-photo",
                    "Content-Type": "image/png",
                },
                content=image.getvalue(),
            )
            assert uploaded.status_code == 201, uploaded.text
            asset = uploaded.json()
            executed = client.post(
                "/local/style-suite/api/runs",
                headers=_headers(),
                json={
                    "product_id": asset["product_id"],
                    "source_asset_id": asset["id"],
                    "idempotency_key": "restart-suite-001",
                },
            )
            assert executed.status_code == 201, executed.text
            run_id = executed.json()["id"]
            state = client.get(
                f"/local/style-suite/api/runs/{run_id}", headers=_headers()
            ).json()
            first = state["review_cards"][0]
            reviewed = client.post(
                f"/v1/image-outputs/{first['job_id']}/candidates/"
                f"{first['candidate_id']}/review",
                headers=_headers(),
                json={
                    "decision": "product_detail_inaccurate",
                    "reason": "restart review",
                    "idempotency_key": "restart-review-001",
                    "fidelity_state": "red",
                    "fidelity_flags": ["product_form_changed"],
                },
            )
            assert reviewed.status_code == 201, reviewed.text

        with _persistent_client(sessions) as restarted:
            recovered = restarted.get(
                f"/local/style-suite/api/runs/{run_id}", headers=_headers()
            )
            assert recovered.status_code == 200, recovered.text
            cards = recovered.json()["review_cards"]
            assert len(cards) == 3
            assert cards[0]["candidate_state"] == "rejected"
            assert cards[0]["fidelity_state"] == "red"
            preview = restarted.get(cards[0]["preview_url"], headers=_headers())
            assert preview.status_code == 200, preview.text
            assert Image.open(BytesIO(preview.content)).format == "JPEG"
    finally:
        engine.dispose()


def test_local_asset_deletion_is_customer_scoped_and_retains_run_lineage(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        image = BytesIO()
        Image.new("RGB", (20, 20), (40, 90, 120)).save(image, format="PNG")
        first = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "delete-free",
                "X-Photo-Id": "delete-free-photo",
                "Content-Type": "image/png",
            },
            content=image.getvalue(),
        ).json()
        denied = client.delete(
            f"/local/style-suite/api/assets/{first['id']}",
            headers=_headers(CUSTOMER_B),
        )
        assert denied.status_code == 404
        deleted = client.delete(
            f"/local/style-suite/api/assets/{first['id']}", headers=_headers()
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["state"] == "deleted"

        prepared = client.post(
            "/local/style-suite/api/synthetic-history", headers=_headers()
        )
        assert prepared.status_code == 201
        _prepared_pilot(client)
        second = client.post(
            "/local/style-suite/api/product",
            headers={
                **_headers(),
                "X-Product-Id": "delete-retained",
                "X-Photo-Id": "delete-retained-photo",
                "Content-Type": "image/png",
            },
            content=image.getvalue(),
        ).json()
        run = client.post(
            "/local/style-suite/api/runs",
            headers=_headers(),
            json={
                "product_id": second["product_id"],
                "source_asset_id": second["id"],
                "idempotency_key": "delete-retained-run",
            },
        )
        assert run.status_code == 201, run.text
        retained = client.delete(
            f"/local/style-suite/api/assets/{second['id']}", headers=_headers()
        )
        assert retained.status_code == 409
        assert retained.json()["error"]["code"] == "local_asset_lineage_retained"
        batch = client.post(
            "/local/style-suite/api/workspace/delete",
            headers=_headers(),
            json={"confirmed": True},
        )
        assert batch.status_code == 207, batch.text
        assert {row["state"] for row in batch.json()["results"]} == {
            "deleted",
            "retained",
        }


def test_real_pilot_requires_exact_cost_confirmation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A changed or absent confirmation must stop before the provider boundary."""
    monkeypatch.setenv("REAL_IMAGE_ENABLED", "true")
    monkeypatch.setenv("REAL_IMAGE_API_KEY", "test-only-key")
    monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "3.00")
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        prepared_pilot = _prepared_pilot(client)
        response = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json={**prepared_pilot, "confirmed_max_usd": "0"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "real_pilot_cost_confirmation_required"
    assert provider_calls == []


def test_real_pilot_requires_confirmed_card_uuid_in_request(tmp_path: Path) -> None:
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        prepared = _prepared_pilot(client)
        prepared.pop("product_identity_card_version_id", None)
        response = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json={**prepared, "confirmed_max_usd": "3.00"},
        )

    assert response.status_code == 422
    assert provider_calls == []


@pytest.mark.parametrize("positive_budget", ["1.00", "3.00", "9.00"])
def test_v1_style_suite_rejects_every_real_budget_without_pilot_capability(
    tmp_path: Path, positive_budget: str
) -> None:
    """No positive price is a substitute for route-issued spending authority."""
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls, budget=positive_budget
        )
        prepared = _prepared_pilot(client)
        response = client.post(
            "/v1/style-suites",
            headers=_headers(),
            json={
                "product_id": prepared["product_id"],
                "source_asset_id": prepared["source_asset_id"],
                "idempotency_key": "legacy-real-full-matrix",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "real_image_pilot_authorization_required"
    assert provider_calls == []


def test_real_pilot_maps_missing_scope_to_non_provider_404(tmp_path: Path) -> None:
    """A bad sealed style identity is a stable local error, never a 500 or call."""
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        payload = {
            **_prepared_pilot(client),
            "scene_style_version_id": str(uuid4()),
            "confirmed_max_usd": "3.00",
        }
        response = client.post(
            "/local/style-suite/api/real-pilot/runs", headers=_headers(), json=payload
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "style_suite_pilot_scope_unavailable"
    assert provider_calls == []


def test_paid_pilot_rejects_demo_style_before_provider_call(tmp_path: Path) -> None:
    """A demo style must never cross the paid-generation boundary."""
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        payload = {**_prepared_demo_pilot(client), "confirmed_max_usd": "3.00"}
        response = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=payload,
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "style_suite_pilot_scope_unavailable"
    assert provider_calls == []


def test_paid_request_separates_product_identity_from_customer_style_evidence(
    tmp_path: Path,
) -> None:
    """The product defines what is sold; history photos only define how it is shot."""
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        payload = {**_prepared_pilot(client), "confirmed_max_usd": "3.00"}
        response = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=payload,
        )
        assert response.status_code == 201, response.text
        confirmed_cards = client.get(
            "/local/style-suite/api/assets/"
            f"{payload['source_asset_id']}/identity-card-versions",
            headers=_headers(),
        )
        assert confirmed_cards.status_code == 200, confirmed_cards.text
        confirmed_card = confirmed_cards.json()[0]
        assert (
            response.json()["product_identity_card_version_id"]
            == payload["product_identity_card_version_id"]
        )
        assert (
            response.json()["product_identity_card_sha256"]
            == confirmed_card["card_sha256"]
        )
        job_id = UUID(response.json()["items"][0]["output_job_id"])

    engine = create_database_engine(f"sqlite:///{tmp_path / 'browser.db'}")
    try:
        with Session(engine) as session:
            job = session.get(ImageOutputJob, job_id)
            assert job is not None
            links = list(
                session.scalars(
                    select(StyleCardSampleLink)
                    .where(
                        StyleCardSampleLink.customer_id == CUSTOMER_A,
                        StyleCardSampleLink.style_card_version_id
                        == UUID(payload["scene_style_version_id"]),
                    )
                    .order_by(StyleCardSampleLink.style_sample_id)
                )
            )
            samples = [session.get(StyleSample, link.style_sample_id) for link in links]
            assert all(sample is not None for sample in samples)
            all_sample_ids = {str(link.style_sample_id) for link in links}
            all_hashes = {sample.content_sha256 for sample in samples if sample}
            metadata = job.request_metadata
    finally:
        engine.dispose()

    assert metadata["product_asset_id"] == payload["source_asset_id"]
    assert (
        metadata["product_identity_card_version_id"]
        == payload["product_identity_card_version_id"]
    )
    assert metadata["product_identity_card_sha256"] == confirmed_card["card_sha256"]
    assert metadata["style_card_version_id"] == payload["scene_style_version_id"]
    assert 4 <= len(metadata["style_sample_ids"]) <= 8
    assert set(metadata["style_sample_ids"]) <= all_sample_ids
    assert set(metadata["style_sample_sha256"]) <= all_hashes
    assert metadata["slot_key"] == payload["slot_key"]
    assert metadata["product_asset_id"] not in metadata["style_sample_ids"]
    assert len(provider_calls) == 1
    body = provider_calls[0].content
    assert body.count(b'name="image[]"') == 5
    assert b'filename="product-identity-1.png"' in body
    assert body.count(b'filename="style-reference-') == 4


def test_real_pilot_uses_startup_generator_once_and_replays_without_charge(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A repeated submission returns the sealed pilot rather than spending again."""
    monkeypatch.setenv("REAL_IMAGE_ENABLED", "true")
    monkeypatch.setenv("REAL_IMAGE_API_KEY", "test-only-key")
    monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "3.00")
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        payload = {**_prepared_pilot(client), "confirmed_max_usd": "3.00"}
        first = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=payload,
        )
        second = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=payload,
        )
        state = client.get(
            f"/local/style-suite/api/runs/{first.json()['id']}", headers=_headers()
        )
        card = state.json()["review_cards"][0]
        output = client.get(f"/v1/image-outputs/{card['job_id']}", headers=_headers())
        content = client.get(card["preview_url"], headers=_headers())

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert state.status_code == 200, state.text
    assert output.status_code == 200, output.text
    assert content.status_code == 200, content.text
    assert len(provider_calls) == 1
    assert first.json()["candidate_count"] == 3
    assert first.json()["publishable"] is False
    assert state.json()["generation_mode"] == "real_model_local_pilot"
    assert state.json()["outbound_attempts"] == 1
    assert state.json()["publishable"] is False
    assert output.json()["synthetic_test_only"] is False
    assert all(
        candidate["synthetic_test_only"] is False
        and candidate["identity"] != "synthetic_style_image_candidate"
        and candidate["publishable"] is False
        for candidate in output.json()["candidates"]
    )
    assert "x-synthetic-artifact" not in content.headers


def test_real_pilot_uses_generator_budget_snapshot_after_environment_drift(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The paid authority is fixed with the generator, not reread per request."""
    monkeypatch.setenv("REAL_IMAGE_ENABLED", "true")
    monkeypatch.setenv("REAL_IMAGE_API_KEY", "test-only-key")
    monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "3.00")
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        payload = {**_prepared_pilot(client), "confirmed_max_usd": "3.00"}
        monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "1.00")
        response = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=payload,
        )

    assert response.status_code == 201, response.text
    assert len(provider_calls) == 1


def test_new_confirmed_pilot_round_gets_one_fresh_single_use_authorization(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A new explicit run key may spend once without reopening an older run."""
    monkeypatch.setenv("REAL_IMAGE_ENABLED", "true")
    monkeypatch.setenv("REAL_IMAGE_API_KEY", "test-only-key")
    monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "3.00")
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        first_payload = {**_prepared_pilot(client), "confirmed_max_usd": "3.00"}
        second_payload = {
            **first_payload,
            "idempotency_key": f"{first_payload['idempotency_key']}-new-round",
        }

        first = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=first_payload,
        )
        second = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=second_payload,
        )
        replay = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=second_payload,
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert replay.status_code == 200, replay.text
    assert first.json()["id"] != second.json()["id"]
    assert second.json()["candidate_count"] == 3
    assert replay.json()["id"] == second.json()["id"]
    assert len(provider_calls) == 2


def test_failed_real_pilot_replay_does_not_redeliver_or_hide_provider_attempts(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A failed paid call is terminal for its sealed key and remains auditable."""
    monkeypatch.setenv("REAL_IMAGE_ENABLED", "true")
    monkeypatch.setenv("REAL_IMAGE_API_KEY", "test-only-key")
    monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "3.00")
    provider_calls: list[httpx.Request] = []
    with _client(tmp_path) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: (
            _failing_real_generator(provider_calls)
        )
        payload = {**_prepared_pilot(client), "confirmed_max_usd": "3.00"}
        first = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=payload,
        )
        replay = client.post(
            "/local/style-suite/api/real-pilot/runs",
            headers=_headers(),
            json=payload,
        )
        state = client.get(
            f"/local/style-suite/api/runs/{first.json()['id']}", headers=_headers()
        )

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["state"] == "failed"
    assert first.json()["items"][0]["state"] == "failed"
    assert first.json()["items"][0]["failure_reason"] == "provider_unavailable"
    assert first.json()["uncertain_child_count"] == 0
    assert first.json()["candidate_count"] == 0
    assert state.status_code == 200, state.text
    assert state.json()["generation_mode"] == "real_model_local_pilot"
    assert state.json()["outbound_attempts"] == 1
    assert len(provider_calls) == 1
