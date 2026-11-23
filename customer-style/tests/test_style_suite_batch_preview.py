"""Signed read-only preview for the bounded real multi-style batch."""
# ruff: noqa: F811

from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_image_jobs_api import (  # noqa: F401
    CUSTOMER_A_ID,
    api_session_factory,
    asset_factory,
    content_store,
)
from test_product_style_output import (
    _confirmed_identity_card,
    _confirmed_identity_observation,
    _historical,
)

from opc_style.api.dependencies import get_session
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.domain.errors import DomainRuleError
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    ImageCandidate,
    ImageOutputJob,
    ProductAsset,
    ProductForegroundVersion,
    ProductIdentityCardVersion,
    ProductIdentityObservationReview,
    StyleReferencePack,
    StyleSuiteRun,
)
from opc_style.services.style_reference_packs import StyleReferencePackService
from opc_style.services.style_suite_batch_scope import SignedBatchScope
from opc_style.services.style_suites import StyleSuiteService
from opc_style.settings import RealImageProviderSettings, StyleSuitePlanningSettings


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
                slot_props=[],
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
        pack_ids = [
            str(row.id)
            for row in session.scalars(
                select(StyleReferencePack).order_by(StyleReferencePack.created_at)
            )
        ]
    return {
        "request": {
            "product_id": asset.product_id,
            "source_asset_id": str(asset.id),
            "product_identity_card_version_id": str(card.id),
            "product_identity_observation_run_id": str(observation.id),
            "product_identity_observation_review_id": None,
            "styles": selections,
        },
        "pack_ids": pack_ids,
    }


@pytest.fixture
def client(
    api_session_factory: Any,
    content_store: Any,
) -> Generator[TestClient, None, None]:
    app = create_app()
    app.state.style_suite_planning_settings = StyleSuitePlanningSettings(
        real_generation_enabled=True,
        candidate_estimate_usd=Decimal("0.12"),
    )

    def override() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_asset_content_store] = lambda: content_store
    with TestClient(app) as test_client:
        yield test_client


def _counts(session: Session) -> tuple[int, ...]:
    return tuple(
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (
            StyleReferencePack,
            StyleSuiteRun,
            ImageOutputJob,
            ImageCandidate,
            AuditEvent,
        )
    )


def test_signed_batch_scope_rejects_tampering() -> None:
    signed = SignedBatchScope.issue(b"test-secret", {"candidate_count": 6})

    assert SignedBatchScope.verify(b"test-secret", signed.token).payload == {
        "candidate_count": 6
    }
    encoded, signature = signed.token.split(".", 1)
    with pytest.raises(DomainRuleError, match="style_suite_preview_invalid"):
        SignedBatchScope.verify(b"test-secret", f"{encoded}x.{signature}")


def test_preview_returns_signed_six_cell_matrix_without_writes(
    client: TestClient,
    batch_fixture: dict[str, Any],
    api_session_factory: Any,
) -> None:
    with api_session_factory() as session:
        before = _counts(session)

    response = client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json=batch_fixture["request"],
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 6
    assert body["candidate_count"] == 6
    assert body["maximum_estimated_cost_usd"] == "0.72"
    assert body["publishable"] is False
    assert body["preview_token"]
    assert [item["style_reference_pack_id"] for item in body["items"]] == [
        pack_id for pack_id in batch_fixture["pack_ids"] for _ in range(2)
    ]
    with api_session_factory() as session:
        assert _counts(session) == before


def test_preview_rejects_scope_above_live_provider_budget_before_execution(
    client: TestClient,
    batch_fixture: dict[str, Any],
    api_session_factory: Any,
) -> None:
    client.app.state.real_image_provider_settings = RealImageProviderSettings(
        enabled=True,
        api_key="test",
        max_run_usd=Decimal("0.30"),
        estimated_request_usd=Decimal("0.12"),
        _env_file=None,
    )
    with api_session_factory() as session:
        before = _counts(session)

    response = client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json=batch_fixture["request"],
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "style_suite_batch_cost_exceeded"
    with api_session_factory() as session:
        assert _counts(session) == before


def test_preview_quotes_with_the_live_provider_reserve_estimate(
    client: TestClient,
    batch_fixture: dict[str, Any],
) -> None:
    client.app.state.real_image_provider_settings = RealImageProviderSettings(
        enabled=True,
        api_key="test",
        max_run_usd=Decimal("0.30"),
        estimated_request_usd=Decimal("0.30"),
        _env_file=None,
    )
    request = {
        **batch_fixture["request"],
        "styles": [
            {
                **batch_fixture["request"]["styles"][0],
                "slot_keys": ["hero"],
            }
        ],
    }

    response = client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json=request,
    )

    assert response.status_code == 200, response.text
    assert response.json()["maximum_estimated_cost_usd"] == "0.30"


def test_real_preview_requires_product_identity_observation(
    client: TestClient,
    batch_fixture: dict[str, Any],
) -> None:
    request = dict(batch_fixture["request"])
    request.pop("product_identity_observation_run_id")

    response = client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json=request,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "product_identity_observation_required"


@pytest.mark.parametrize(
    ("decision", "reason_codes", "error_code"),
    [
        ("needs_review", ["visibility_limited"], "product_identity_observation_review_required"),
        ("block", ["core_accessory_extra"], "product_identity_observation_blocked"),
    ],
)
def test_real_preview_rejects_unresolved_or_blocked_observation(
    client: TestClient,
    batch_fixture: dict[str, Any],
    api_session_factory: Any,
    decision: str,
    reason_codes: list[str],
    error_code: str,
) -> None:
    request = dict(batch_fixture["request"])
    with api_session_factory() as session:
        asset = session.get(ProductAsset, UUID(request["source_asset_id"]))
        card = session.get(
            ProductIdentityCardVersion,
            UUID(request["product_identity_card_version_id"]),
        )
        assert asset is not None and card is not None
        observation = _confirmed_identity_observation(
            session,
            asset,
            card,
            decision=decision,
            reason_codes=reason_codes,
        )
        request["product_identity_observation_run_id"] = str(observation.id)

    response = client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json=request,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == error_code


def test_real_preview_accepts_customer_approved_review(
    client: TestClient,
    batch_fixture: dict[str, Any],
    api_session_factory: Any,
) -> None:
    request = dict(batch_fixture["request"])
    with api_session_factory() as session:
        asset = session.get(ProductAsset, UUID(request["source_asset_id"]))
        card = session.get(
            ProductIdentityCardVersion,
            UUID(request["product_identity_card_version_id"]),
        )
        assert asset is not None and card is not None
        observation = _confirmed_identity_observation(
            session,
            asset,
            card,
            decision="needs_review",
            reason_codes=["visibility_limited"],
        )
        review = ProductIdentityObservationReview(
            customer_id=CUSTOMER_A_ID,
            observation_run_id=observation.id,
            product_asset_id=asset.id,
            product_id=asset.product_id,
            source_content_sha256=asset.content_sha256,
            product_identity_card_version_id=card.id,
            product_identity_card_sha256=card.card_sha256,
            observation_result_sha256=observation.result_sha256,
            decision="approved",
            reason="扣头被遮挡，已由客户人工核对",
        )
        session.add(review)
        session.commit()
        request["product_identity_observation_run_id"] = str(observation.id)
        request["product_identity_observation_review_id"] = str(review.id)

    response = client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json=request,
    )

    assert response.status_code == 200, response.text


def test_confirmed_mask_routes_supported_slot_to_pixel_preserving_mode(
    client: TestClient,
    batch_fixture: dict[str, Any],
    api_session_factory: Any,
    content_store: Any,
) -> None:
    asset_id = UUID(batch_fixture["request"]["source_asset_id"])
    with api_session_factory() as session:
        asset = session.get(ProductAsset, asset_id)
        assert asset is not None
        foreground = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        ImageDraw.Draw(foreground).ellipse((6, 6, 25, 25), fill=(200, 20, 20, 255))
        mask = foreground.getchannel("A")
        foreground_buffer, mask_buffer = BytesIO(), BytesIO()
        foreground.save(foreground_buffer, "PNG")
        mask.save(mask_buffer, "PNG")
        foreground_bytes, mask_bytes = (
            foreground_buffer.getvalue(),
            mask_buffer.getvalue(),
        )
        version = ProductForegroundVersion(
            customer_id=CUSTOMER_A_ID,
            product_asset_id=asset.id,
            product_id=asset.product_id,
            version=1,
            source_content_sha256=asset.content_sha256,
            foreground_sha256=sha256(foreground_bytes).hexdigest(),
            mask_sha256=sha256(mask_bytes).hexdigest(),
            width=32,
            height=32,
            extractor_version="test-v1",
            manifest_sha256="f" * 64,
            state="confirmed",
            quality_flags=[],
            confirmation_key="confirmed-mask",
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

    request = {
        **batch_fixture["request"],
        "styles": [
            {
                **batch_fixture["request"]["styles"][0],
                "slot_keys": ["detail"],
            }
        ],
    }
    response = client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json=request,
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["render_mode"] == "pixel_preserving_isolated"
    assert item["pixel_preserving_eligible"] is True
    assert item["foreground_version_id"] == str(version.id)
    assert item["provider_image_count"] in {2, 3, 4}
    assert item["background_candidate_count"] == 1
    assert item["blocking_code"] is None


@pytest.mark.parametrize("props", [None, [], ["none"]])
def test_legacy_no_props_variants_require_pixel_preserving_mask(
    api_session_factory: Any,
    props: list[str] | None,
) -> None:
    with api_session_factory() as session:
        rules: dict[str, object] = {
            "camera_angle": "front",
        }
        if props is not None:
            rules["props"] = props
        eligible, blocking_code = StyleSuiteService(
            session
        )._pixel_preserving_eligibility(
            foreground=None,
            identity_payload={},
            slot_key="detail",
            slot_rules=rules,
        )
    assert eligible is False
    assert blocking_code == "pixel_preserving_mask_required"


@pytest.mark.parametrize(
    ("slot_key", "camera_angle", "props", "expected"),
    [
        ("hero", "front", ["present"], "pixel_preserving_mask_required"),
        ("detail", "overhead", ["present"], "pixel_preserving_mask_required"),
        ("wear", "front", ["present"], "pixel_preserving_ineligible"),
        ("hero", "back", [], "pixel_preserving_ineligible"),
    ],
)
def test_pixel_preserving_policy_allows_decorative_background_props_but_not_pose_changes(
    api_session_factory: Any,
    slot_key: str,
    camera_angle: str,
    props: list[str],
    expected: str,
) -> None:
    with api_session_factory() as session:
        eligible, blocking_code = StyleSuiteService(
            session
        )._pixel_preserving_eligibility(
            foreground=None,
            identity_payload={},
            slot_key=slot_key,
            slot_rules={"camera_angle": camera_angle, "props": props},
        )
    assert eligible is False
    assert blocking_code == expected


@pytest.mark.parametrize("style_count,duplicate_slots", [(4, False), (3, True)])
def test_preview_rejects_scope_above_hard_ceiling(
    client: TestClient,
    batch_fixture: dict[str, Any],
    style_count: int,
    duplicate_slots: bool,
) -> None:
    styles = list(batch_fixture["request"]["styles"])
    if style_count == 4:
        styles.append(
            {
                **styles[0],
                "scene_style_version_id": "11111111-1111-1111-1111-111111111111",
            }
        )
    if duplicate_slots:
        styles = [
            {**row, "slot_keys": ["detail", "detail", "detail"]} for row in styles
        ]

    response = client.post(
        "/local/style-suite/api/batch-runs/preview",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json={**batch_fixture["request"], "styles": styles},
    )

    assert response.status_code in {409, 422}
