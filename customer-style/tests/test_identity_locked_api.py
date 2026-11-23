"""HTTP contract for the one-click identity-locked product style flow."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.api.routes_image_jobs import (
    get_background_generator,
    get_identity_subject_segmenter,
)
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.domain.subject_seal import DeterministicLocalSubjectSegmenter
from opc_style.main import create_app
from opc_style.models import (
    Base,
    Customer,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.providers.identity_openai_backgrounds import OpenAIBackgroundGenerator
from opc_style.providers.openai_images import RealImageBudget
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.settings import RealImageProviderSettings

CUSTOMER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FOREIGN = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@contextmanager
def _client(
    tmp_path: Path,
    *,
    background_generator: object | None = None,
    real_style_ready: bool = False,
) -> Generator[tuple[TestClient, SceneStyle, StyleCardVersion], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'identity-api.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sessions() as session:
        customer = Customer(id=CUSTOMER, display_name="API test")
        card = StyleCard(customer_id=CUSTOMER, kind="derived", active=True)
        session.add(customer)
        session.flush()
        session.add(card)
        session.flush()
        version_id = uuid4()
        scene_id = uuid4()
        slot_id = uuid4()
        ready_rules = {
            "aspect_ratio": "4:5",
            "shot_distance": "close",
            "camera_angle": "front",
        }
        version = StyleCardVersion(
            id=version_id,
            customer_id=CUSTOMER,
            style_card_id=card.id,
            version=1,
            content=(
                {
                    "overrides": {
                        "palette": ["warm"],
                        "lighting": ["soft"],
                        "backgrounds": ["plain"],
                        "composition": ["centered"],
                    },
                    "slots": [
                        {
                            "id": str(slot_id),
                            "customer_id": str(CUSTOMER),
                            "scene_style_id": str(scene_id),
                            "style_card_version_id": str(version_id),
                            "slot_key": "hero",
                            "label": "主图",
                            "position": 0,
                            "active": True,
                            "rules": ready_rules,
                        }
                    ],
                }
                if real_style_ready
                else {"lighting": "soft daylight"}
            ),
        )
        scene = SceneStyle(
            id=scene_id,
            customer_id=CUSTOMER,
            style_card_id=card.id,
            name="tabletop",
            active=True,
            rules={"surface": "stone"},
        )
        session.add_all([version, scene])
        session.flush()
        if real_style_ready:
            session.add(
                StyleTemplateSlot(
                    id=slot_id,
                    customer_id=CUSTOMER,
                    scene_style_id=scene.id,
                    style_card_version_id=version.id,
                    slot_key="hero",
                    label="主图",
                    rules=ready_rules,
                    active=True,
                    position=0,
                )
            )
            for index in range(10):
                sample = StyleSample(
                    customer_id=CUSTOMER,
                    photo_id=f"ready-{index}",
                    authorized_at=datetime.now(UTC),
                    consent_provenance="test",
                    consent_text_version="v1",
                    content_sha256=f"{index:064x}",
                    mime_type="image/png",
                    width=10,
                    height=10,
                    asset_store_namespace="test-ready",
                )
                session.add(sample)
                session.flush()
                session.add(
                    StyleCardSampleLink(
                        customer_id=CUSTOMER,
                        style_card_version_id=version.id,
                        style_sample_id=sample.id,
                    )
                )
        session.commit()
    store = LocalSyntheticAssetStore(tmp_path / "media")
    app = create_app()

    def session_override() -> Generator[Session, None, None]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_asset_content_store] = lambda: store
    app.dependency_overrides[get_identity_subject_segmenter] = (
        DeterministicLocalSubjectSegmenter
    )
    if background_generator is not None:
        app.dependency_overrides[get_background_generator] = lambda: background_generator
    try:
        with TestClient(app) as client:
            yield client, scene, version
    finally:
        store.close()
        engine.dispose()


def _png() -> bytes:
    output = BytesIO()
    image = Image.new("RGBA", (12, 8), (0, 0, 0, 0))
    for x in range(2, 10):
        image.putpixel((x, 4), (190, 25, 20, 255))
    image.save(output, format="PNG")
    return output.getvalue()


def test_one_click_create_replay_refresh_and_candidate_content(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, scene, version):
        headers = {"X-Customer-Id": str(CUSTOMER)}
        uploaded = client.post(
            "/local/style-suite/api/product",
            headers={
                **headers,
                "X-Product-Id": "PRODUCT-API-001",
                "X-Photo-Id": "PHOTO-API-001",
                "Content-Type": "image/png",
            },
            content=_png(),
        )
        assert uploaded.status_code == 201, uploaded.text
        body = {
            "sources": [
                {
                    "product_id": uploaded.json()["product_id"],
                    "asset_id": uploaded.json()["id"],
                }
            ],
            "scene_style_id": str(scene.id),
            "style_card_version_id": str(version.id),
            "output_ratio": "4:5",
            "idempotency_key": "identity-api-001",
        }

        preview = client.post(
            "/v1/image-outputs/product-style/identity-locked/preview",
            headers=headers,
            json=body,
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["generation_mode"] == "synthetic_test_only"
        assert preview.json()["candidate_count"] == 3
        assert preview.json()["maximum_usd"] == "0"
        assert preview.json()["quote_kind"] == "no_provider_charge"
        assert preview.json()["automatic_retry"] is False
        assert preview.json()["sends_product_image_to_provider"] is False
        assert len(preview.json()["manifest_sha256"]) == 64

        created = client.post(
            "/v1/image-outputs/product-style/identity-locked",
            headers=headers,
            json=body,
        )
        replay = client.post(
            "/v1/image-outputs/product-style/identity-locked",
            headers=headers,
            json=body,
        )
        refreshed = client.get(
            f"/v1/image-outputs/{created.json()['id']}", headers=headers
        )

        assert created.status_code == 201, created.text
        assert replay.status_code == 200, replay.text
        assert refreshed.status_code == 200, refreshed.text
        assert created.json()["id"] == replay.json()["id"] == refreshed.json()["id"]
        assert created.json()["subject_lock_state"] == "locked"
        assert created.json()["subject_seal_id"]
        assert len(created.json()["candidates"]) == 3
        assert all(
            candidate["protected_pixels_match"] is True
            for candidate in created.json()["candidates"]
        )
        candidate = created.json()["candidates"][0]
        content = client.get(
            f"/v1/image-outputs/{created.json()['id']}/candidates/"
            f"{candidate['id']}/content",
            headers=headers,
        )
        assert content.status_code == 200
        assert content.headers["content-type"] == "image/png"
        assert content.headers["x-synthetic-artifact"] == "true"
        signed_content = client.get(candidate["content_url"])
        assert signed_content.status_code == 200
        assert signed_content.content == content.content
        assert client.get(
            candidate["content_url"],
            headers={"X-Customer-Id": str(FOREIGN)},
        ).status_code == 404
        assert client.get(candidate["content_url"] + "x").status_code == 404
        original = client.get(
            f"/v1/image-outputs/{created.json()['id']}/original-content",
            headers=headers,
        )
        assert original.status_code == 200
        assert original.headers["content-type"] == "image/png"
        assert original.content == _png()
        signed_original = client.get(created.json()["original_content_url"])
        assert signed_original.status_code == 200
        assert signed_original.content == original.content
        checked = client.post(
            f"/v1/image-outputs/{created.json()['id']}/authenticity-checks",
            headers=headers,
        )
        assert checked.status_code == 201, checked.text
        assert checked.json()["status"] == "awaiting_customer_review"
        reviewed = client.post(
            f"/v1/image-outputs/{created.json()['id']}/candidates/"
            f"{candidate['id']}/review",
            headers=headers,
            json={
                "decision": "approved",
                "reason": None,
                "idempotency_key": "identity-review-001",
                "fidelity_state": "green",
                "fidelity_flags": [],
            },
        )
        assert reviewed.status_code == 201, reviewed.text
        assert reviewed.json()["customer_approved"] is True
        default_view = client.get(
            f"/v1/products/{uploaded.json()['product_id']}/images", headers=headers
        )
        real_view = client.get(
            f"/v1/products/{uploaded.json()['product_id']}/images?view=real",
            headers=headers,
        )
        assert default_view.status_code == real_view.status_code == 200
        assert default_view.json()["resolved_view"] == "style_image"
        assert real_view.json()["resolved_view"] == "real_photo"
        assert client.get(
            f"/v1/image-outputs/{created.json()['id']}",
            headers={"X-Customer-Id": str(FOREIGN)},
        ).status_code == 404


def test_subject_mask_gate_confirms_and_binds_reviewed_seal(tmp_path: Path) -> None:
    """Paid-ready generation must be able to reuse one human-confirmed mask."""
    with _client(tmp_path) as (client, scene, version):
        headers = {"X-Customer-Id": str(CUSTOMER)}
        uploaded = client.post(
            "/local/style-suite/api/product",
            headers={
                **headers,
                "X-Product-Id": "PRODUCT-MASK-001",
                "X-Photo-Id": "PHOTO-MASK-001",
                "Content-Type": "image/png",
            },
            content=_png(),
        ).json()
        proposed = client.post(
            "/v1/product-subjects/preview",
            headers=headers,
            json={"asset_id": uploaded["id"]},
        )
        assert proposed.status_code == 201, proposed.text
        assert proposed.json()["review_state"] == "needs_human_review"
        rgba = client.get(proposed.json()["rgba_url"], headers=headers)
        assert rgba.status_code == 200
        assert client.get(
            proposed.json()["rgba_url"],
            headers={"X-Customer-Id": str(FOREIGN)},
        ).status_code == 404
        with Image.open(BytesIO(rgba.content)) as subject:
            mask = subject.getchannel("A")
            mask_output = BytesIO()
            mask.save(mask_output, format="PNG")
        confirmed = client.post(
            f"/v1/product-subjects/{proposed.json()['id']}/confirm",
            headers={
                **headers,
                "Content-Type": "image/png",
                "X-Confirmation-Key": "mask-api-001",
                "X-Confirmed-Subject-Mask": "true",
            },
            content=mask_output.getvalue(),
        )
        assert confirmed.status_code == 201, confirmed.text
        assert confirmed.json()["review_state"] == "confirmed"
        body = {
            "sources": [
                {"product_id": uploaded["product_id"], "asset_id": uploaded["id"]}
            ],
            "scene_style_id": str(scene.id),
            "style_card_version_id": str(version.id),
            "subject_seal_id": confirmed.json()["id"],
            "output_ratio": "4:5",
            "idempotency_key": "identity-mask-api-001",
        }
        created = client.post(
            "/v1/image-outputs/product-style/identity-locked",
            headers=headers,
            json=body,
        )
        assert created.status_code == 201, created.text
        assert created.json()["subject_seal_id"] == confirmed.json()["id"]


def test_real_background_route_rejects_unconfirmed_cost_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    monkeypatch.setenv("REAL_IMAGE_ENABLED", "true")
    monkeypatch.setenv("REAL_IMAGE_API_KEY", "secret-value")
    monkeypatch.setenv("REAL_IMAGE_MODEL", "gpt-image-2")
    monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "0.36")
    settings = RealImageProviderSettings()
    generator = OpenAIBackgroundGenerator(
        settings,
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("0.36")),
    )

    with _client(
        tmp_path,
        background_generator=generator,
        real_style_ready=True,
    ) as (client, scene, version):
        headers = {"X-Customer-Id": str(CUSTOMER)}
        uploaded = client.post(
            "/local/style-suite/api/product",
            headers={
                **headers,
                "X-Product-Id": "PRODUCT-REAL-COST",
                "X-Photo-Id": "PHOTO-REAL-COST",
                "Content-Type": "image/png",
            },
            content=_png(),
        ).json()
        proposed = client.post(
            "/v1/product-subjects/preview",
            headers=headers,
            json={"asset_id": uploaded["id"]},
        ).json()
        rgba = client.get(proposed["rgba_url"], headers=headers).content
        with Image.open(BytesIO(rgba)) as subject:
            mask = subject.getchannel("A")
            mask_output = BytesIO()
            mask.save(mask_output, format="PNG")
        reviewed = client.post(
            f"/v1/product-subjects/{proposed['id']}/confirm",
            headers={
                **headers,
                "Content-Type": "image/png",
                "X-Confirmation-Key": "real-cost-mask-001",
                "X-Confirmed-Subject-Mask": "true",
            },
            content=mask_output.getvalue(),
        ).json()
        body = {
            "sources": [
                {
                    "product_id": uploaded["product_id"],
                    "asset_id": uploaded["id"],
                }
            ],
            "scene_style_id": str(scene.id),
            "style_card_version_id": str(version.id),
            "subject_seal_id": reviewed["id"],
            "output_ratio": "4:5",
            "slot_key": "hero",
            "idempotency_key": "identity-real-confirmation-001",
        }
        preview = client.post(
            "/v1/image-outputs/product-style/identity-locked/preview",
            headers=headers,
            json=body,
        )
        assert preview.status_code == 200, preview.text
        quote = preview.json()
        assert quote["generation_mode"] == "real_background_local_composite"
        assert quote["model"] == "gpt-image-2"
        assert quote["maximum_usd"] == "0.36"
        assert (
            quote["quote_kind"]
            == "local_authorization_cap_not_provider_invoice_guarantee"
        )

        missing = client.post(
            "/v1/image-outputs/product-style/identity-locked",
            headers=headers,
            json=body,
        )
        wrong_manifest = client.post(
            "/v1/image-outputs/product-style/identity-locked",
            headers={
                **headers,
                "X-Confirmed-Maximum-Usd": quote["maximum_usd"],
                "X-Confirmed-Manifest-Sha256": "0" * 64,
            },
            json=body,
        )

        assert missing.status_code == 409
        assert wrong_manifest.status_code == 409
        assert missing.json()["error"]["code"] == (
            "identity_locked_cost_confirmation_required"
        )
        assert wrong_manifest.json()["error"]["code"] == (
            "identity_locked_cost_confirmation_required"
        )
        assert calls == 0


def test_browser_cannot_choose_provider_mask_placement_or_publish_flag(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as (client, scene, version):
        response = client.post(
            "/v1/image-outputs/product-style/identity-locked",
            headers={"X-Customer-Id": str(CUSTOMER)},
            json={
                "sources": [{"product_id": "P", "asset_id": str(uuid4())}],
                "scene_style_id": str(scene.id),
                "style_card_version_id": str(version.id),
                "output_ratio": "4:5",
                "idempotency_key": "forbidden-controls",
                "provider": "paid",
                "mask": "caller-mask",
                "placement": {"x": 0, "y": 0},
                "publishable": True,
            },
        )

        assert response.status_code == 422
