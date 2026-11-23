"""Authorized execution for one signed six-cell style batch."""
# ruff: noqa: F811

from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_image_jobs_api import (  # noqa: F401
    CUSTOMER_A_ID,
    api_session_factory,
    asset_factory,
    content_store,
)
from test_openai_image_provider import _settings, _success_payload
from test_product_style_output import (
    _confirmed_identity_card,
    _confirmed_identity_observation,
    _historical,
)
from test_real_fidelity_authorization import RecordingFidelityAssessor

from opc_style.api.dependencies import get_session
from opc_style.api.routes_image_jobs import get_image_generator
from opc_style.api.routes_local_style_suite import _resolve_pixel_background_item
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.domain.background_generator import (
    BackgroundGenerationRequest,
    GeneratedBackground,
)
from opc_style.main import create_app
from opc_style.models import (
    PixelBackgroundRun,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductForegroundVersion,
    StyleSuiteRunItem,
)
from opc_style.providers.openai_images import OpenAIImageGenerator, RealImageBudget
from opc_style.services.style_reference_packs import StyleReferencePackService
from opc_style.settings import (
    RealBackgroundProviderSettings,
    RealFidelitySettings,
    StyleSuitePlanningSettings,
)


class RecordingBackgroundGenerator:
    adapter_version = "test-background-v1"
    model = "gpt-image-2"
    publishable = False

    def __init__(self) -> None:
        self.calls: list[BackgroundGenerationRequest] = []

    def generate(self, request: BackgroundGenerationRequest) -> GeneratedBackground:
        self.calls.append(request)
        output = BytesIO()
        Image.new("RGB", (1024, 1024), "beige").save(output, "PNG")
        content = output.getvalue()
        return GeneratedBackground(
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/png",
            width=1024,
            height=1024,
        )


@pytest.fixture
def provider_calls() -> list[httpx.Request]:
    return []


@pytest.fixture
def batch_fixture(
    api_session_factory: Any,
    content_store: Any,
    asset_factory: Any,
) -> dict[str, Any]:
    asset = asset_factory()
    selections = []
    with api_session_factory() as session:
        card = _confirmed_identity_card(session, asset)
        observation = _confirmed_identity_observation(session, asset, card)
        for index in range(3):
            style, _ = _historical(
                session,
                content_store,
                count=10,
                name=f"真实风格 {index + 1}",
                slot_keys=("detail", "hero"),
                slot_props=["display_prop"],
                slot_camera_angle="rear",
            )
            StyleReferencePackService(session).get_or_create(
                CUSTOMER_A_ID, style.version.id
            )
            selections.append(
                {
                    "scene_style_version_id": str(style.version.id),
                    "slot_keys": ["detail", "hero"],
                }
            )
        isolated_style, _ = _historical(
            session,
            content_store,
            count=10,
            name="像素保真风格",
            slot_keys=("detail",),
            slot_props=[],
        )
        StyleReferencePackService(session).get_or_create(
            CUSTOMER_A_ID, isolated_style.version.id
        )
    return {
        "request": {
            "product_id": asset.product_id,
            "source_asset_id": str(asset.id),
            "product_identity_card_version_id": str(card.id),
            "product_identity_observation_run_id": str(observation.id),
            "product_identity_observation_review_id": None,
            "styles": selections,
        },
        "observation_id": str(observation.id),
        "observation_result_sha256": observation.result_sha256,
        "isolated_selection": {
            "scene_style_version_id": str(isolated_style.version.id),
            "slot_keys": ["detail"],
        },
    }


def test_execute_rejects_isolated_scope_without_confirmed_foreground(
    real_client: TestClient,
    batch_fixture: dict[str, Any],
    provider_calls: list[httpx.Request],
) -> None:
    headers = {"X-Customer-Id": str(CUSTOMER_A_ID)}
    selected = {
        **batch_fixture["request"],
        "styles": [batch_fixture["isolated_selection"]],
    }
    preview = real_client.post(
        "/local/style-suite/api/batch-runs/preview", headers=headers, json=selected
    )
    assert preview.status_code == 200
    assert preview.json()["items"][0]["blocking_code"] == (
        "pixel_preserving_mask_required"
    )

    response = real_client.post(
        "/local/style-suite/api/batch-runs",
        headers=headers,
        json={
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": "missing-foreground-must-not-dispatch",
            "confirmed_maximum_usd": preview.json()["maximum_estimated_cost_usd"],
            "external_processing_confirmed": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pixel_preserving_mask_required"
    assert provider_calls == []


@pytest.fixture
def real_client(
    api_session_factory: Any,
    content_store: Any,
    provider_calls: list[httpx.Request],
) -> Generator[TestClient, None, None]:
    def handler(wire: httpx.Request) -> httpx.Response:
        provider_calls.append(wire)
        payload = _success_payload()
        payload["data"] = payload["data"][:1]  # type: ignore[index]
        return httpx.Response(200, request=wire, json=payload)

    generator = OpenAIImageGenerator(
        _settings(budget="3.00"),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("3.00")),
        estimated_request_usd=Decimal("0.12"),
    )
    app = create_app()
    app.state.style_suite_planning_settings = StyleSuitePlanningSettings(
        real_generation_enabled=True,
        candidate_estimate_usd=Decimal("0.12"),
    )
    app.state.real_background_settings = RealBackgroundProviderSettings(
        enabled=True, api_key="test", max_run_usd=Decimal("0.12")
    )
    app.state.background_generator = RecordingBackgroundGenerator()

    def override() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_asset_content_store] = lambda: content_store
    app.dependency_overrides[get_image_generator] = lambda: generator
    with TestClient(app) as test_client:
        yield test_client


def test_execute_seals_six_items_and_dispatches_each_once(
    real_client: TestClient,
    batch_fixture: dict[str, Any],
    provider_calls: list[httpx.Request],
) -> None:
    headers = {"X-Customer-Id": str(CUSTOMER_A_ID)}
    preview = real_client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers=headers,
        json=batch_fixture["request"],
    )
    assert preview.status_code == 200
    execution = {
        "preview_token": preview.json()["preview_token"],
        "idempotency_key": "six-cell-first-round-v1",
        "confirmed_maximum_usd": preview.json()["maximum_estimated_cost_usd"],
        "external_processing_confirmed": True,
    }

    first = real_client.post(
        "/local/style-suite/api/batch-runs", headers=headers, json=execution
    )
    replay = real_client.post(
        "/local/style-suite/api/batch-runs", headers=headers, json=execution
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert first.json()["candidate_count"] == 6, first.text
    assert (
        first.json()["product_identity_observation_run_id"]
        == batch_fixture["observation_id"]
    )
    assert (
        first.json()["product_identity_observation_result_sha256"]
        == batch_fixture["observation_result_sha256"]
    )
    assert first.json()["product_identity_observation_review_id"] is None
    assert len(first.json()["items"]) == 6
    assert len(provider_calls) == 6

    restored = real_client.get(
        f"/local/style-suite/api/batch-runs/{first.json()['id']}",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    matrix = restored.json()["matrix"]
    assert len(matrix) == 3
    assert all(len(style["slots"]) == 2 for style in matrix)
    assert all(
        cell["provider_style_reference_attachment_count"] >= 2
        for style in matrix
        for cell in style["slots"]
    )
    assert all(
        len(cell["candidates"]) == 1 for style in matrix for cell in style["slots"]
    )


def test_permission_change_after_preview_blocks_before_provider_dispatch(
    real_client: TestClient,
    batch_fixture: dict[str, Any],
    api_session_factory: Any,
    provider_calls: list[httpx.Request],
) -> None:
    headers = {"X-Customer-Id": str(CUSTOMER_A_ID)}
    preview = real_client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers=headers,
        json=batch_fixture["request"],
    )
    assert preview.status_code == 200, preview.text
    with api_session_factory() as session:
        asset_id = UUID(batch_fixture["request"]["source_asset_id"])
        session.add(
            ProductAssetPermissionEvent(
                customer_id=CUSTOMER_A_ID,
                product_asset_id=asset_id,
                transform_permission=False,
                occurred_at=datetime.now(UTC),
                provenance="test_permission_withdrawal",
            )
        )
        session.commit()

    execution = real_client.post(
        "/local/style-suite/api/batch-runs",
        headers=headers,
        json={
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": "withdrawn-after-preview",
            "confirmed_maximum_usd": preview.json()["maximum_estimated_cost_usd"],
            "external_processing_confirmed": True,
        },
    )

    assert execution.status_code == 409
    assert execution.json()["error"]["code"] == "transform_permission_denied"
    assert provider_calls == []


def test_pixel_cell_generates_empty_background_then_composes_locally(
    real_client: TestClient,
    batch_fixture: dict[str, Any],
    api_session_factory: Any,
    content_store: Any,
    provider_calls: list[httpx.Request],
) -> None:
    asset_id = UUID(batch_fixture["request"]["source_asset_id"])
    with api_session_factory() as session:
        asset = session.get(ProductAsset, asset_id)
        assert asset is not None
        foreground = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(foreground).ellipse((8, 8, 55, 55), fill=(180, 20, 20, 255))
        mask = foreground.getchannel("A")
        foreground_io, mask_io = BytesIO(), BytesIO()
        foreground.save(foreground_io, "PNG")
        mask.save(mask_io, "PNG")
        foreground_bytes, mask_bytes = foreground_io.getvalue(), mask_io.getvalue()
        version = ProductForegroundVersion(
            customer_id=CUSTOMER_A_ID,
            product_asset_id=asset.id,
            product_id=asset.product_id,
            version=1,
            source_content_sha256=asset.content_sha256,
            foreground_sha256=sha256(foreground_bytes).hexdigest(),
            mask_sha256=sha256(mask_bytes).hexdigest(),
            width=64,
            height=64,
            extractor_version="test-v1",
            manifest_sha256="e" * 64,
            pipeline_manifest_sha256="d" * 64,
            stage_receipts_sha256="c" * 64,
            state="confirmed",
            quality_flags=[],
            confirmation_key="pixel-confirmed",
            decided_at=datetime.now(UTC),
        )
        session.add(version)
        session.commit()
        content_store.persist_product_foreground(
            CUSTOMER_A_ID, version.id, foreground_bytes, version.foreground_sha256
        )
        content_store.persist_product_mask(
            CUSTOMER_A_ID, version.id, mask_bytes, version.mask_sha256
        )

    headers = {"X-Customer-Id": str(CUSTOMER_A_ID)}
    selected = {
        **batch_fixture["request"],
        "styles": [batch_fixture["isolated_selection"]],
    }
    preview = real_client.post(
        "/local/style-suite/api/batch-runs/preview", headers=headers, json=selected
    )
    assert preview.status_code == 200
    assert preview.json()["items"][0]["render_mode"] == "pixel_preserving_isolated"
    batch = real_client.post(
        "/local/style-suite/api/batch-runs",
        headers=headers,
        json={
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": "pixel-batch-v1",
            "confirmed_maximum_usd": "0.12",
            "external_processing_confirmed": True,
        },
    )
    assert batch.status_code == 201, batch.text
    assert provider_calls == []
    with api_session_factory() as session:
        item = session.scalar(
            select(StyleSuiteRunItem).where(
                StyleSuiteRunItem.run_id == UUID(batch.json()["id"])
            )
        )
        assert item is not None and item.state == "pixel_background_planned"
        item_id = item.id

    background_preview = real_client.post(
        "/local/style-suite/api/pixel-backgrounds/preview",
        headers=headers,
        json={
            "style_suite_run_id": batch.json()["id"],
            "style_suite_run_item_id": str(item_id),
            "foreground_version_id": str(version.id),
        },
    )
    assert background_preview.status_code == 200, background_preview.text
    generated = real_client.post(
        "/local/style-suite/api/pixel-background-runs",
        headers=headers,
        json={
            "preview_token": background_preview.json()["preview_token"],
            "idempotency_key": "pixel-background-v1",
            "confirmed_maximum_usd": "0.12",
        },
    )
    assert generated.status_code == 201, generated.text
    assert (generated.json()["output_width"], generated.json()["output_height"]) == (
        1024,
        1536,
    )
    background_id = generated.json()["id"]
    background_generator = real_client.app.state.background_generator
    assert isinstance(background_generator, RecordingBackgroundGenerator)
    assert background_generator.calls[-1].output_size == (1024, 1536)
    with api_session_factory() as session:
        background_row = session.get(PixelBackgroundRun, UUID(background_id))
        assert background_row is not None
        assert background_row.authorized_manifest["style_suite_run_item_id"] == str(
            item_id
        )
        assert (
            background_row.authorized_manifest["background_adapter_version"]
            == background_generator.adapter_version
        )
    background_content = real_client.get(
        f"/local/style-suite/api/pixel-background-runs/{background_id}/content",
        headers=headers,
    )
    assert background_content.status_code == 200
    assert background_content.headers["content-type"] == "image/png"
    confirmed = real_client.post(
        f"/local/style-suite/api/pixel-background-runs/{background_id}/decision",
        headers=headers,
        json={"decision": "confirmed"},
    )
    assert confirmed.status_code == 200
    recommendation = real_client.get(
        f"/local/style-suite/api/pixel-background-runs/{background_id}/compose-recommendation",
        headers=headers,
    )
    assert recommendation.status_code == 200, recommendation.text
    assert recommendation.json()["publishable"] is False
    assert recommendation.json()["save_allowed"] is True
    assert recommendation.json()["placement_state"] == "ready"
    assert recommendation.json()["category_calibration_state"] in {
        "calibrated",
        "pilot_calibrated",
        "legacy_pilot",
    }
    assert recommendation.json()["reason_codes"] == [
        "tabletop_horizon_detected",
        "foreground_mask_fitted",
    ]
    preview_composition = real_client.post(
        f"/local/style-suite/api/pixel-background-runs/{background_id}/compose-preview",
        headers=headers,
        json={
            "translate_y": 12,
            "uniform_scale": "0.45",
            "output_width": generated.json()["output_width"],
            "output_height": generated.json()["output_height"],
        },
    )
    assert preview_composition.status_code == 200, preview_composition.text
    assert preview_composition.headers["content-type"] == "image/png"
    assert preview_composition.headers["x-preview-only"] == "true"
    assert len(preview_composition.headers["x-result-sha256"]) == 64
    with api_session_factory() as session:
        previewed_item = session.get(StyleSuiteRunItem, item_id)
        assert previewed_item is not None
        assert previewed_item.output_job_id is None
    composed = real_client.post(
        f"/local/style-suite/api/pixel-background-runs/{background_id}/compose",
        headers=headers,
        json={
            "uniform_scale": "0.45",
            "output_width": generated.json()["output_width"],
            "output_height": generated.json()["output_height"],
        },
    )
    assert composed.status_code == 200, composed.text
    assert composed.json()["publishable"] is False
    restored_pixel_batch = real_client.get(
        f"/local/style-suite/api/batch-runs/{batch.json()['id']}", headers=headers
    )
    assert restored_pixel_batch.status_code == 200, restored_pixel_batch.text
    cell = restored_pixel_batch.json()["matrix"][0]["slots"][0]
    assert cell["item_id"] == str(item_id)
    assert cell["render_mode"] == "pixel_preserving_isolated"
    assert cell["provider_product_attachment_count"] == 0
    assert cell["state"] == "succeeded"
    assert len(cell["candidates"]) == 1
    pixel_candidate = real_client.get(cell["candidates"][0]["preview_url"])
    assert pixel_candidate.status_code == 200
    assert pixel_candidate.headers["content-type"] == "image/png"

    # A reviewed item can request a new empty background without rerunning the
    # product pipeline. This is the bounded commercial-art-direction iteration.
    replacement_preview = real_client.post(
        "/local/style-suite/api/pixel-backgrounds/preview",
        headers=headers,
        json={
            "style_suite_run_id": batch.json()["id"],
            "style_suite_run_item_id": str(item_id),
            "foreground_version_id": str(version.id),
        },
    )
    assert replacement_preview.status_code == 200, replacement_preview.text

    recomposed = real_client.post(
        f"/local/style-suite/api/pixel-background-runs/{background_id}/compose",
        headers=headers,
        json={
            "translate_y": -16,
            "uniform_scale": "0.45",
            "output_width": generated.json()["output_width"],
            "output_height": generated.json()["output_height"],
        },
    )
    assert recomposed.status_code == 200, recomposed.text
    assert recomposed.json()["candidate_id"] != composed.json()["candidate_id"]

    rejected_recomposition = real_client.post(
        f"/v1/image-outputs/{cell['candidates'][0]['job_id']}/candidates/"
        f"{recomposed.json()['candidate_id']}/review",
        headers=headers,
        json={
            "decision": "rejected",
            "reason": "placement calibration alternative not selected",
            "idempotency_key": "reject-pixel-recomposition-v1",
            "fidelity_state": "green",
            "fidelity_flags": [],
        },
    )
    assert rejected_recomposition.status_code == 201, rejected_recomposition.text
    assert rejected_recomposition.json()["candidate_state"] == "customer_rejected"

    quoted_item = preview.json()["items"][0]
    assert len(quoted_item["style_reference_thumbnail_urls"]) >= 2
    thumbnail = real_client.get(quoted_item["style_reference_thumbnail_urls"][0])
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] in {"image/jpeg", "image/png"}


def test_pixel_background_item_binding_wins_when_slot_rules_are_identical() -> None:
    context_id, hero_id = UUID(int=101), UUID(int=102)
    shared_rules = {"aspect_ratio": "1:1"}
    items = [
        SimpleNamespace(
            id=context_id,
            state="pixel_background_planned",
            slot_key="context",
            style_reference_pack_id=UUID(int=201),
            slot_snapshot={"rules": shared_rules},
        ),
        SimpleNamespace(
            id=hero_id,
            state="pixel_background_planned",
            slot_key="hero",
            style_reference_pack_id=UUID(int=201),
            slot_snapshot={"rules": shared_rules},
        ),
    ]
    background = SimpleNamespace(
        authorized_manifest={
            "style_suite_run_item_id": str(hero_id),
            "style_reference_pack_id": str(UUID(int=201)),
            "slot_rules": shared_rules,
        },
        idempotency_key="unrelated-key",
    )

    resolved = _resolve_pixel_background_item(items, background)  # type: ignore[arg-type]

    assert resolved is items[1]


def test_legacy_pixel_background_uses_slot_key_instead_of_equal_rules() -> None:
    context_id, hero_id = UUID(int=301), UUID(int=302)
    shared_rules = {"aspect_ratio": "1:1"}
    items = [
        SimpleNamespace(
            id=context_id,
            state="pixel_background_planned",
            slot_key="context",
            style_reference_pack_id=UUID(int=401),
            slot_snapshot={"rules": shared_rules},
        ),
        SimpleNamespace(
            id=hero_id,
            state="pixel_background_planned",
            slot_key="hero",
            style_reference_pack_id=UUID(int=401),
            slot_snapshot={"rules": shared_rules},
        ),
    ]
    background = SimpleNamespace(
        authorized_manifest={
            "style_reference_pack_id": str(UUID(int=401)),
            "slot_rules": shared_rules,
        },
        idempotency_key="pixel-background-hero-existing-run",
    )

    resolved = _resolve_pixel_background_item(items, background)  # type: ignore[arg-type]

    assert resolved is items[1]


def test_changed_confirmed_price_fails_before_dispatch(
    real_client: TestClient,
    batch_fixture: dict[str, Any],
    provider_calls: list[httpx.Request],
) -> None:
    headers = {"X-Customer-Id": str(CUSTOMER_A_ID)}
    preview = real_client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers=headers,
        json=batch_fixture["request"],
    ).json()

    response = real_client.post(
        "/local/style-suite/api/batch-runs",
        headers=headers,
        json={
            "preview_token": preview["preview_token"],
            "idempotency_key": "changed-price-v1",
            "confirmed_maximum_usd": "0.71",
            "external_processing_confirmed": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "style_suite_preview_expired"
    assert provider_calls == []


def test_three_item_batch_can_run_one_authorized_fidelity_check(
    real_client: TestClient,
    batch_fixture: dict[str, Any],
    provider_calls: list[httpx.Request],
) -> None:
    """A three-style first round must not be stranded without review evidence."""
    headers = {"X-Customer-Id": str(CUSTOMER_A_ID)}
    request = {
        **batch_fixture["request"],
        "styles": [
            {**style, "slot_keys": [style["slot_keys"][0]]}
            for style in batch_fixture["request"]["styles"]
        ],
    }
    preview = real_client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers=headers,
        json=request,
    )
    generated = real_client.post(
        "/local/style-suite/api/batch-runs",
        headers=headers,
        json={
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": "three-style-first-round-v1",
            "confirmed_maximum_usd": preview.json()["maximum_estimated_cost_usd"],
            "external_processing_confirmed": True,
        },
    )
    assert generated.status_code == 201, generated.text
    assert len(provider_calls) == 3

    assessor = RecordingFidelityAssessor()
    real_client.app.state.real_fidelity_settings = RealFidelitySettings(
        enabled=True,
        api_key="test-secret",
        max_run_usd="0.50",
        _env_file=None,
    )
    real_client.app.state.real_fidelity_assessor = assessor
    run_id = generated.json()["id"]
    quote = real_client.get(
        f"/local/style-suite/api/runs/{run_id}/fidelity-preview",
        headers=headers,
    )

    assert quote.status_code == 200, quote.text
    assert quote.json()["candidate_count"] == 3
    checked = real_client.post(
        f"/local/style-suite/api/runs/{run_id}/fidelity-checks",
        headers=headers,
        json={
            "idempotency_key": "three-style-fidelity-v1",
            "confirmed_maximum_usd": quote.json()["maximum_usd"],
            "confirmed_manifest_sha256": quote.json()["input_manifest_sha256"],
        },
    )
    assert checked.status_code == 201, checked.text
    assert assessor.calls == 1

    restored = real_client.get(f"/local/style-suite/api/runs/{run_id}", headers=headers)
    assert restored.status_code == 200, restored.text
    assert [card["fidelity_state"] for card in restored.json()["review_cards"]] == [
        "red",
        "green",
        "unknown",
    ]
