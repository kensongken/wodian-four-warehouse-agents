"""Local-only consumer API for producer-confirmed classification handoffs."""

import base64
import json
from collections.abc import Generator
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.api.routes_image_jobs import get_image_generator
from opc_style.contracts.classification_handoff import (
    ClassificationStyleHandoffV1,
    canonical_handoff_bytes,
)
from opc_style.db import create_database_engine
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    Base,
    ClassificationBridgeClaim,
    Customer,
    ImageCandidate,
    ImageOutputJob,
    ProductAsset,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
)
from opc_style.providers.openai_images import OpenAIImageGenerator, RealImageBudget
from opc_style.schemas import ProductAssetIntakeRequest
from opc_style.services.assets import ProductAssetService
from opc_style.services.classification_bridge import (
    ClassificationBridgeService,
    _customer_id,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.settings import LocalBridgeSettings, RealImageProviderSettings


def _jpeg_bytes() -> bytes:
    body = BytesIO()
    Image.new("RGB", (48, 32), (20, 90, 160)).save(body, format="JPEG")
    return body.getvalue()


def _payload(*, receipt_id: str, content_sha256: str) -> dict[str, object]:
    photo_id = "21111111-1111-4111-8111-111111111111"
    return {
        "schema_version": "classification-style-handoff-v1",
        "handoff_id": "11111111-1111-4111-8111-111111111111",
        "customer_test_id": "test-customer-001",
        "product_id": "test-product-001",
        "product_category": "turquoise",
        "product_form": "bracelet",
        "classification_version": "classification-v7",
        "confirmation_version": 3,
        "main_photo_id": photo_id,
        "transform_permission": True,
        "photos": [
            {
                "photo_id": photo_id,
                "content_sha256": content_sha256,
                "mime_type": "image/jpeg",
                "width": 48,
                "height": 32,
                "automatic_category": "real_photo",
                "confidence": "high",
                "human_corrected": False,
                "confirmed_category": "real_photo",
                "media_receipt_id": receipt_id,
            }
        ],
        "idempotency_key": "style-test:test-product-001:v3",
        "desensitized_test_only": True,
        "source": "classification_local_page",
        "publishable": False,
        "production_eligible": False,
    }


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'bridge-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _enabled_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
    *,
    host: str = "127.0.0.1",
) -> TestClient:
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", "test-bridge-nonce")
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_BRIDGE_STAGE_ROOT", str(tmp_path / "bridge-media")
    )
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_BRIDGE_ASSET_ROOT", str(tmp_path / "bridge-assets")
    )
    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(
        app,
        client=(host, 51000),
        headers={"X-Local-Classification-Bridge-Nonce": "test-bridge-nonce"},
    )


def _real_generator(provider_calls: list[httpx.Request]) -> OpenAIImageGenerator:
    candidate = base64.b64encode(_jpeg_bytes()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"b64_json": candidate}] * 3},
        )

    return OpenAIImageGenerator(
        RealImageProviderSettings(
            enabled=True,
            api_key="test-only-key",
            max_run_usd=Decimal("1.00"),
        ),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal("1.00")),
    )


def _active_style_pin(
    api_session_factory: sessionmaker[Session],
    *,
    customer_test_id: str = "test-customer-001",
) -> None:
    customer_id = _customer_id(customer_test_id)
    with api_session_factory() as session:
        session.add(Customer(id=customer_id, display_name="Local bridge test"))
        main = StyleCard(customer_id=customer_id, kind="main", active=True)
        session.add(main)
        session.flush()
        main_version = StyleCardVersion(
            customer_id=customer_id,
            style_card_id=main.id,
            version=1,
            content={"tone": ["synthetic"]},
        )
        session.add(main_version)
        scene_card = StyleCard(customer_id=customer_id, kind="scene", active=True)
        session.add(scene_card)
        session.flush()
        scene_version = StyleCardVersion(
            customer_id=customer_id,
            style_card_id=scene_card.id,
            version=1,
            content={"main_style_card_version_id": str(main_version.id)},
        )
        session.add(scene_version)
        session.add(
            SceneStyle(
                customer_id=customer_id,
                style_card_id=scene_card.id,
                name="Local bridge scene",
                active=True,
            )
        )
        session.commit()


def _seed_second_ready_product(api_session_factory: sessionmaker[Session]) -> None:
    customer_id = _customer_id("test-customer-001")
    with api_session_factory() as session:
        ProductAssetService(session).intake(
            customer_id,
            ProductAssetIntakeRequest.model_construct(
                product_id="test-product-002",
                photo_id=str(uuid4()),
                confirmed=True,
                source_type="synthetic_metadata",
                quality_flags=["classification_local_test"],
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                transform_permission=True,
                content_sha256="e" * 64,
                mime_type="image/jpeg",
                width=48,
                height=32,
            ),
        )


def test_disabled_bridge_has_the_same_absence_semantics_as_a_missing_route() -> None:
    response = TestClient(create_app()).post(
        "/v1/local-test/classification-bridge/media"
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("stage_root", "asset_root", "expected_code"),
    [
        (Path("relative-stage"), Path("/tmp/strict-assets"), "absolute_roots"),
        (Path("/tmp/strict-stage"), None, "asset_root_required"),
        (
            Path("/tmp/strict-root/../strict-stage"),
            Path("/tmp/strict-assets"),
            "canonical_roots",
        ),
        (
            Path("/private/tmp/strict-root/stage"),
            Path("/private/tmp/strict-root"),
            "roots_overlap",
        ),
    ],
)
def test_enabled_bridge_requires_explicit_disjoint_absolute_storage_roots(
    stage_root: Path,
    asset_root: Path | None,
    expected_code: str,
) -> None:
    """Weakening either configured path boundary must fail this test."""
    with pytest.raises(ValidationError, match=expected_code):
        LocalBridgeSettings(
            _env_file=None,
            local_classification_bridge=True,
            local_classification_bridge_nonce="strict-test-nonce",
            local_classification_bridge_stage_root=stage_root,
            local_classification_bridge_asset_root=asset_root,
        )


def test_enabled_bridge_stages_and_replays_a_confirmed_real_photo_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """Removing receipt attestation or replay matching must fail this test."""
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        staged = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        )
        assert staged.status_code == 201
        receipt = staged.json()
        payload = _payload(
            receipt_id=receipt["receipt_id"], content_sha256=receipt["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = receipt["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = receipt["photo_id"]
        handoff = canonical_handoff_bytes(
            ClassificationStyleHandoffV1.model_validate(payload)
        )

        created = client.post(
            "/v1/local-test/classification-bridge/handoffs", content=handoff
        )
        replayed = client.post(
            "/v1/local-test/classification-bridge/handoffs", content=handoff
        )

    assert created.status_code == 201
    assert created.json()["created"] is True
    assert created.json()["publishable"] is False
    assert created.json()["status"] == "awaiting_review"
    assert len(created.json()["output_job_ids"]) == 1
    assert replayed.status_code == 200
    assert replayed.json() == {**created.json(), "created": False}


def test_enabled_app_configuration_reopens_bridge_artifacts_for_public_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Removing app-owned DB/store wiring or reopen support must fail this test."""
    database_path = tmp_path / "configured-bridge.db"
    database_url = f"sqlite:///{database_path}"
    setup_engine = create_database_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_engine.dispose()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", "configured-nonce")
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_BRIDGE_STAGE_ROOT", str(tmp_path / "configured-stage")
    )
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_BRIDGE_ASSET_ROOT", str(tmp_path / "configured-assets")
    )

    body = _jpeg_bytes()
    first_app = create_app()
    with TestClient(
        first_app,
        client=("127.0.0.1", 51000),
        headers={"X-Local-Classification-Bridge-Nonce": "configured-nonce"},
        raise_server_exceptions=False,
    ) as first_client:
        staged = first_client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        )
        assert staged.status_code == 201
        receipt = staged.json()
        payload = _payload(
            receipt_id=receipt["receipt_id"],
            content_sha256=receipt["content_sha256"],
        )
        payload["photos"][0]["photo_id"] = receipt["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = receipt["photo_id"]
        accepted = first_client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(
                ClassificationStyleHandoffV1.model_validate(payload)
            ),
        )
        assert accepted.status_code == 201, accepted.text
        job_id = UUID(accepted.json()["output_job_ids"][0])

    inspection_engine = create_database_engine(database_url)
    inspection_factory = sessionmaker(
        bind=inspection_engine, expire_on_commit=False, class_=Session
    )
    with inspection_factory() as session:
        candidate = (
            session.query(ImageCandidate)
            .filter_by(output_job_id=job_id, sequence_number=1)
            .one()
        )
        candidate_id = candidate.id
    inspection_engine.dispose()

    restarted_app = create_app()
    with TestClient(
        restarted_app,
        client=("127.0.0.1", 51001),
        raise_server_exceptions=False,
    ) as restarted_client:
        review = restarted_client.post(
            f"/v1/image-outputs/{job_id}/candidates/{candidate_id}/review",
            headers={"X-Customer-Id": str(_customer_id("test-customer-001"))},
            json={
                "decision": "approved",
                "reason": "restart-safe local bridge review",
                "idempotency_key": "configured-bridge-review",
            },
        )

    assert review.status_code == 201, review.text
    assert review.json()["job_status"] == "customer_approved"
    assert review.json()["publishable"] is False


def test_enabled_bridge_rejects_non_loopback_media_before_accepting_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    body = _jpeg_bytes()
    with _enabled_client(
        monkeypatch, tmp_path, api_session_factory, host="203.0.113.10"
    ) as client:
        response = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "local_classification_bridge_loopback_required",
            "retryable": False,
        }
    }


@pytest.mark.parametrize(
    ("nonce", "expected_status", "expected_code"),
    [
        ("", 401, "local_classification_bridge_nonce_required"),
        ("not-the-startup-nonce", 403, "local_classification_bridge_nonce_invalid"),
    ],
)
def test_enabled_bridge_requires_the_ephemeral_startup_nonce_before_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
    nonce: str,
    expected_status: int,
    expected_code: str,
) -> None:
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        response = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Local-Classification-Bridge-Nonce": nonce,
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        )

    assert response.status_code == expected_status
    assert response.json() == {"error": {"code": expected_code, "retryable": False}}


def test_enabled_bridge_rejects_a_wrong_nonce_before_parsing_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=b'{"not":"a-valid-handoff"}',
            headers={"X-Local-Classification-Bridge-Nonce": "wrong"},
        )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "local_classification_bridge_nonce_invalid",
            "retryable": False,
        }
    }


def test_enabled_bridge_rejects_noncanonical_handoff_before_consuming_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=json.dumps({"unknown": "field"}),
        )

    assert response.status_code == 422


def test_missing_receipt_proof_is_retryable_and_never_creates_an_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """Changing receipt resolution to accept absent evidence must fail this test."""
    payload = _payload(receipt_id=str(uuid4()), content_sha256="a" * 64)
    handoff = canonical_handoff_bytes(
        ClassificationStyleHandoffV1.model_validate(payload)
    )
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs", content=handoff
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "local_media_receipt_unavailable", "retryable": True}
    }


def test_conflicting_retry_with_the_same_handoff_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        staged = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=staged["receipt_id"], content_sha256=staged["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = staged["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = staged["photo_id"]
        accepted = canonical_handoff_bytes(
            ClassificationStyleHandoffV1.model_validate(payload)
        )
        assert (
            client.post(
                "/v1/local-test/classification-bridge/handoffs", content=accepted
            ).status_code
            == 201
        )
        conflicting = deepcopy(payload)
        conflicting["product_form"] = "ring"
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(
                ClassificationStyleHandoffV1.model_validate(conflicting)
            ),
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "classification_handoff_idempotency_conflict",
            "retryable": False,
        }
    }


def test_wear_category_stays_pending_without_creating_a_wear_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """Routing wear_photo into an image job must fail this pending-only test."""
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        real = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        wear = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=real["receipt_id"], content_sha256=real["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = real["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = real["photo_id"]
        wear_photo = deepcopy(payload["photos"][0])  # type: ignore[index]
        wear_photo["photo_id"] = wear["photo_id"]
        wear_photo["media_receipt_id"] = wear["receipt_id"]
        wear_photo["automatic_category"] = "wear_photo"
        wear_photo["confirmed_category"] = "wear_photo"
        payload["photos"].append(wear_photo)  # type: ignore[index]
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(
                ClassificationStyleHandoffV1.model_validate(payload)
            ),
        )

    assert response.status_code == 201
    assert response.json()["status"] == "pending_real_model"
    assert len(response.json()["output_job_ids"]) == 1


def test_confirmed_real_and_wear_photos_only_register_assets_and_link_suite_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    body = _jpeg_bytes()
    provider_calls: list[httpx.Request] = []
    _active_style_pin(api_session_factory)
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        staged = [
            client.post(
                "/v1/local-test/classification-bridge/media",
                content=body,
                headers={
                    "X-Photo-Id": str(uuid4()),
                    "X-Content-Sha256": sha256(body).hexdigest(),
                },
            ).json()
            for _ in range(2)
        ]
        payload = _payload(
            receipt_id=staged[0]["receipt_id"],
            content_sha256=staged[0]["content_sha256"],
        )
        payload["photos"][0]["photo_id"] = staged[0]["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = staged[0]["photo_id"]
        wear_photo = deepcopy(payload["photos"][0])  # type: ignore[index]
        wear_photo["photo_id"] = staged[1]["photo_id"]
        wear_photo["media_receipt_id"] = staged[1]["receipt_id"]
        wear_photo["automatic_category"] = "wear_photo"
        wear_photo["confirmed_category"] = "wear_photo"
        payload["photos"].append(wear_photo)  # type: ignore[union-attr]
        body_bytes = canonical_handoff_bytes(
            ClassificationStyleHandoffV1.model_validate(payload)
        )

        first = client.post(
            "/v1/local-test/classification-bridge/handoffs", content=body_bytes
        )
        replay = client.post(
            "/v1/local-test/classification-bridge/handoffs", content=body_bytes
        )

    assert first.status_code == 201, first.text
    assert first.json()["status"] == "needs_confirmation", (
        first.json(),
        len(provider_calls),
    )
    assert first.json()["output_job_ids"] == []
    assert first.json()["suite_preview_url"].startswith(
        "/local/style-suite?product_id=test-product-001&source_asset_id="
    )
    assert replay.status_code == 200
    assert replay.json()["output_job_ids"] == first.json()["output_job_ids"]
    assert provider_calls == []
    with api_session_factory() as session:
        assert list(session.scalars(select(ImageOutputJob))) == []
        originals = list(session.scalars(select(ProductAsset)))
        assert len(originals) == 2  # Keep all originals; only confirmed main defines identity.
        primary = next(asset for asset in originals if asset.photo_id == staged[0]["photo_id"])
        assert str(primary.id) in first.json()["suite_preview_url"]


def test_missing_transform_permission_never_invokes_real_generator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    body = _jpeg_bytes()
    provider_calls: list[httpx.Request] = []
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        client.app.dependency_overrides[get_image_generator] = lambda: _real_generator(
            provider_calls
        )
        staged = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=staged["receipt_id"],
            content_sha256=staged["content_sha256"],
        )
        payload["transform_permission"] = False
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=json.dumps(payload, separators=(",", ":")),
        )

    assert response.status_code == 422
    assert provider_calls == []


def test_cleanup_pending_retry_reuses_the_same_batch_bound_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """A later failure must be resumable without a second upload or new receipt."""
    body = _jpeg_bytes()
    original_route = ClassificationBridgeService._route_categories
    calls = 0

    def fail_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic downstream failure")
        return original_route(self, *args, **kwargs)

    monkeypatch.setattr(ClassificationBridgeService, "_route_categories", fail_once)
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        staged = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=staged["receipt_id"], content_sha256=staged["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = staged["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = staged["photo_id"]
        handoff = canonical_handoff_bytes(
            ClassificationStyleHandoffV1.model_validate(payload)
        )

        failed = client.post(
            "/v1/local-test/classification-bridge/handoffs", content=handoff
        )
        pending = client.get(
            "/v1/local-test/classification-bridge/handoffs/"
            "11111111-1111-4111-8111-111111111111"
        )
        resumed = client.post(
            "/v1/local-test/classification-bridge/handoffs", content=handoff
        )

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "classification_handoff_cleanup_pending"
    assert pending.status_code == 200
    assert pending.json()["status"] == "needs_confirmation"
    assert resumed.status_code == 201
    assert resumed.json()["created"] is True


def test_completed_handoff_get_and_replay_survive_an_app_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as first_client:
        staged = first_client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=staged["receipt_id"], content_sha256=staged["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = staged["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = staged["photo_id"]
        handoff = canonical_handoff_bytes(
            ClassificationStyleHandoffV1.model_validate(payload)
        )
        created = first_client.post(
            "/v1/local-test/classification-bridge/handoffs", content=handoff
        ).json()

    with _enabled_client(
        monkeypatch, tmp_path, api_session_factory
    ) as restarted_client:
        replay = restarted_client.post(
            "/v1/local-test/classification-bridge/handoffs", content=handoff
        )
        fetched = restarted_client.get(
            f"/v1/local-test/classification-bridge/handoffs/{created['handoff_id']}"
        )

    assert replay.status_code == 200
    assert replay.json() == {**created, "created": False}
    assert fetched.status_code == 200
    assert fetched.json() == {**created, "created": False}


def test_local_derivative_namespace_reopens_only_when_explicitly_requested(
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-derivatives"
    initial = LocalSyntheticAssetStore(root)
    initial.close()

    reopened = LocalSyntheticAssetStore(root, reopen=True)
    assert reopened.store_identity == "local-synthetic-asset-store-v1"
    reopened.close()


def test_later_confirmation_reuses_the_same_real_asset_without_losing_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    body = _jpeg_bytes()
    photo_id = str(uuid4())
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        first_receipt = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": photo_id,
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        first = _payload(
            receipt_id=first_receipt["receipt_id"],
            content_sha256=first_receipt["content_sha256"],
        )
        first["photos"][0]["photo_id"] = photo_id  # type: ignore[index]
        first["main_photo_id"] = photo_id
        assert (
            client.post(
                "/v1/local-test/classification-bridge/handoffs",
                content=canonical_handoff_bytes(
                    ClassificationStyleHandoffV1.model_validate(first)
                ),
            ).status_code
            == 201
        )
        client.app.state.classification_bridge_stage.cleanup_expired(
            now=datetime(2026, 8, 31, tzinfo=UTC).replace(year=2027)
        )

        second_receipt = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": photo_id,
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        second = deepcopy(first)
        second["handoff_id"] = "51111111-1111-4111-8111-111111111111"
        second["confirmation_version"] = 4
        second["idempotency_key"] = "style-test:test-product-001:v4"
        second["photos"][0]["media_receipt_id"] = second_receipt["receipt_id"]  # type: ignore[index]
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(
                ClassificationStyleHandoffV1.model_validate(second)
            ),
        )

    with api_session_factory() as session:
        assets = session.query(ProductAsset).all()
    assert response.status_code == 201
    assert len(assets) == 1


def test_non_real_main_photo_is_rejected_before_asset_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        staged = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        staged_real = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=staged["receipt_id"], content_sha256=staged["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = staged["photo_id"]  # type: ignore[index]
        payload["photos"][0]["automatic_category"] = "scene_photo"  # type: ignore[index]
        payload["photos"][0]["confirmed_category"] = "scene_photo"  # type: ignore[index]
        payload["main_photo_id"] = staged["photo_id"]
        # Keep the contract's required real-photo fact while asking the bridge to
        # route the additional, producer-confirmed scene category.
        real_photo = deepcopy(payload["photos"][0])  # type: ignore[index]
        real_photo["photo_id"] = staged_real["photo_id"]
        real_photo["media_receipt_id"] = staged_real["receipt_id"]
        real_photo["automatic_category"] = "real_photo"
        real_photo["confirmed_category"] = "real_photo"
        payload["photos"].append(real_photo)  # type: ignore[index]
        handoff = canonical_handoff_bytes(
            ClassificationStyleHandoffV1.model_validate(payload)
        )
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs", content=handoff
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "classification_handoff_main_real_photo_required",
            "retryable": False,
        }
    }
    with api_session_factory() as session:
        assert list(session.scalars(select(ProductAsset))) == []


def test_configured_synthetic_product_fails_authenticity_inside_bridge_and_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """Bypassing the bridge or returning a passing fixture must fail this test."""
    monkeypatch.setenv(
        "LOCAL_CLASSIFICATION_BRIDGE_AUTHENTICITY_FAILURE_PRODUCT_ID",
        "test-product-authenticity-failure",
    )
    _active_style_pin(api_session_factory)
    real_body = _jpeg_bytes()
    scene_buffer = BytesIO()
    Image.new("RGB", (48, 32), (170, 40, 30)).save(scene_buffer, format="JPEG")
    scene_body = scene_buffer.getvalue()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        real = client.post(
            "/v1/local-test/classification-bridge/media",
            content=real_body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(real_body).hexdigest(),
            },
        ).json()
        scene = client.post(
            "/v1/local-test/classification-bridge/media",
            content=scene_body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(scene_body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=real["receipt_id"], content_sha256=real["content_sha256"]
        )
        payload["product_id"] = "test-product-authenticity-failure"
        payload["idempotency_key"] = "style-test:test-product-authenticity-failure:v3"
        payload["photos"][0]["photo_id"] = real["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = real["photo_id"]
        scene_photo = deepcopy(payload["photos"][0])  # type: ignore[index]
        scene_photo.update(
            {
                "photo_id": scene["photo_id"],
                "content_sha256": scene["content_sha256"],
                "automatic_category": "scene_photo",
                "confirmed_category": "scene_photo",
                "media_receipt_id": scene["receipt_id"],
            }
        )
        payload["photos"].append(scene_photo)  # type: ignore[index]
        accepted = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(
                ClassificationStyleHandoffV1.model_validate(payload)
            ),
        )

        with api_session_factory() as session:
            failed_job = (
                session.query(ImageOutputJob).filter_by(output_type="scene").one()
            )
            candidate = (
                session.query(ImageCandidate)
                .filter_by(output_job_id=failed_job.id, sequence_number=1)
                .one()
            )
            failed_job_id = failed_job.id
            candidate_id = candidate.id
            failed_status = failed_job.status

        review = client.post(
            f"/v1/image-outputs/{failed_job_id}/candidates/{candidate_id}/review",
            headers={"X-Customer-Id": str(_customer_id("test-customer-001"))},
            json={
                "decision": "approved",
                "reason": "must remain blocked",
                "idempotency_key": "authenticity-failure-review",
            },
        )

    assert accepted.status_code == 201
    assert accepted.json()["status"] == "needs_confirmation"
    assert failed_status == "authenticity_failed"
    assert review.status_code == 409
    assert review.json()["error"]["code"] == "review_job_not_awaiting_customer"


def test_scene_category_with_an_active_pin_reaches_human_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    _active_style_pin(api_session_factory)
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        real = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        scene = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=real["receipt_id"], content_sha256=real["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = real["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = real["photo_id"]
        scene_photo = deepcopy(payload["photos"][0])  # type: ignore[index]
        scene_photo["photo_id"] = scene["photo_id"]
        scene_photo["media_receipt_id"] = scene["receipt_id"]
        scene_photo["automatic_category"] = "scene_photo"
        scene_photo["confirmed_category"] = "scene_photo"
        payload["photos"].append(scene_photo)  # type: ignore[index]
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(
                ClassificationStyleHandoffV1.model_validate(payload)
            ),
        )

    assert response.status_code == 201
    assert response.json()["status"] == "awaiting_review"
    assert len(response.json()["output_job_ids"]) == 2


def test_multi_category_with_a_pin_but_one_product_is_retryable_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    _active_style_pin(api_session_factory)
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        real = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        multi = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=real["receipt_id"], content_sha256=real["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = real["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = real["photo_id"]
        multi_photo = deepcopy(payload["photos"][0])  # type: ignore[index]
        multi_photo["photo_id"] = multi["photo_id"]
        multi_photo["media_receipt_id"] = multi["receipt_id"]
        multi_photo["automatic_category"] = "multi_product_photo"
        multi_photo["confirmed_category"] = "multi_product_photo"
        payload["photos"].append(multi_photo)  # type: ignore[index]
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(
                ClassificationStyleHandoffV1.model_validate(payload)
            ),
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "local_multi_product_sources_required", "retryable": True}
    }


def test_stale_owner_cleanup_cas_cannot_write_a_cleanup_audit(
    tmp_path: Path, api_session_factory: sessionmaker[Session]
) -> None:
    """An old worker may not downgrade another owner's durable claim."""
    customer_id = _customer_id("test-customer-001")
    handoff = ClassificationStyleHandoffV1.model_validate(
        _payload(receipt_id=str(uuid4()), content_sha256="a" * 64)
    )
    with api_session_factory() as session:
        session.add(Customer(id=customer_id, display_name="Local bridge"))
        session.flush()
        claim = ClassificationBridgeClaim(
            customer_id=customer_id,
            handoff_id=handoff.handoff_id,
            idempotency_key=handoff.idempotency_key,
            handoff_digest=sha256(canonical_handoff_bytes(handoff)).hexdigest(),
            state="started",
            owner_token="owner-b",
            lease_expires_at=datetime.now(UTC),
        )
        session.add(claim)
        session.commit()
        service = ClassificationBridgeService(
            session,
            _stage_for_test(tmp_path),
            lambda: LocalSyntheticAssetStore(tmp_path / "store"),
        )
        service._record_cleanup_pending(
            customer_id, handoff, "a" * 64, RuntimeError("stale"), "jobs", []
        )
        session.refresh(claim)
        assert claim.owner_token == "owner-b"
        assert (
            session.query(AuditEvent)
            .filter(
                AuditEvent.event_type == "classification_bridge.handoff_cleanup_pending"
            )
            .count()
            == 0
        )


def test_legacy_cleanup_claim_is_taken_over_and_resumed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """A pre-lease cleanup claim remains recoverable by its exact handoff."""
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        staged = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=staged["receipt_id"], content_sha256=staged["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = staged["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = staged["photo_id"]
        handoff = ClassificationStyleHandoffV1.model_validate(payload)
        customer_id = _customer_id(handoff.customer_test_id)
        with api_session_factory() as session:
            session.add(Customer(id=customer_id, display_name="Local bridge"))
            session.flush()
            session.add(
                ClassificationBridgeClaim(
                    customer_id=customer_id,
                    handoff_id=handoff.handoff_id,
                    idempotency_key=handoff.idempotency_key,
                    handoff_digest=sha256(canonical_handoff_bytes(handoff)).hexdigest(),
                    state="cleanup",
                    owner_token=None,
                    lease_expires_at=None,
                )
            )
            session.commit()
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(handoff),
        )

    assert response.status_code == 201
    assert response.json()["status"] == "awaiting_review"


def _seed_bridge_owner_claim(  # type: ignore[no-untyped-def]
    session: Session, service: ClassificationBridgeService, handoff
) -> UUID:
    customer_id = _customer_id(handoff.customer_test_id)
    session.add(Customer(id=customer_id, display_name="Local bridge"))
    session.flush()
    session.add(
        ClassificationBridgeClaim(
            customer_id=customer_id,
            handoff_id=handoff.handoff_id,
            idempotency_key=handoff.idempotency_key,
            handoff_digest=sha256(canonical_handoff_bytes(handoff)).hexdigest(),
            state="started",
            owner_token=service._owner_token,
            lease_expires_at=datetime.now(UTC),
        )
    )
    session.commit()
    return customer_id


@pytest.mark.parametrize("status", ["awaiting_customer_review", "authenticity_failed"])
def test_existing_authenticity_terminal_routes_record_already_invoked_phase(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """Existing checked jobs must not call authenticity again after restart."""
    handoff = ClassificationStyleHandoffV1.model_validate(
        _payload(receipt_id=str(uuid4()), content_sha256="b" * 64)
    )
    with api_session_factory() as session:
        service = ClassificationBridgeService(
            session,
            _stage_for_test(tmp_path),
            lambda: LocalSyntheticAssetStore(tmp_path / "store"),
        )
        customer_id = _seed_bridge_owner_claim(session, service, handoff)
        job = SimpleNamespace(id=uuid4(), status=status)
        monkeypatch.setattr(
            service, "_existing_or_real_output", lambda *args: SimpleNamespace(job=job)
        )
        photo = handoff.photos[0]
        service._route_categories(
            customer_id,
            handoff,
            {photo.photo_id: SimpleNamespace(asset=SimpleNamespace(id=uuid4()))},
            record_job=lambda job_id, phase: service._record_step(
                customer_id, handoff, "b" * 64, phase, job_id
            ),
        )
        phases = [
            event.payload["phase"]
            for event in session.query(AuditEvent).filter(
                AuditEvent.event_type == "classification_bridge.step"
            )
        ]

    assert phases == ["job_committed", "authenticity_already_invoked"]


def test_restart_compensated_terminal_routes_record_without_authenticity_phase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """A non-machine-check recovery state is visibly compensation, not a check."""
    handoff = ClassificationStyleHandoffV1.model_validate(
        _payload(receipt_id=str(uuid4()), content_sha256="c" * 64)
    )
    with api_session_factory() as session:
        service = ClassificationBridgeService(
            session,
            _stage_for_test(tmp_path),
            lambda: LocalSyntheticAssetStore(tmp_path / "store"),
        )
        customer_id = _seed_bridge_owner_claim(session, service, handoff)
        job = SimpleNamespace(id=uuid4(), status="cleanup_abandoned")
        monkeypatch.setattr(
            service, "_existing_or_real_output", lambda *args: SimpleNamespace(job=job)
        )
        photo = handoff.photos[0]
        service._route_categories(
            customer_id,
            handoff,
            {photo.photo_id: SimpleNamespace(asset=SimpleNamespace(id=uuid4()))},
            record_job=lambda job_id, phase: service._record_step(
                customer_id, handoff, "c" * 64, phase, job_id
            ),
        )
        phases = [
            event.payload["phase"]
            for event in session.query(AuditEvent).filter(
                AuditEvent.event_type == "classification_bridge.step"
            )
        ]

    assert phases == ["job_committed", "terminalized_without_authenticity"]


def test_authenticity_invocation_failure_routes_to_cleanup_with_failed_phase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    """A real machine-check invocation failure emits its failure phase before cleanup."""
    handoff = ClassificationStyleHandoffV1.model_validate(
        _payload(receipt_id=str(uuid4()), content_sha256="d" * 64)
    )
    with api_session_factory() as session:
        service = ClassificationBridgeService(
            session,
            _stage_for_test(tmp_path),
            lambda: LocalSyntheticAssetStore(tmp_path / "store"),
        )
        customer_id = _seed_bridge_owner_claim(session, service, handoff)
        job = SimpleNamespace(id=uuid4(), status="machine_check")
        monkeypatch.setattr(
            service, "_existing_or_real_output", lambda *args: SimpleNamespace(job=job)
        )
        from opc_style.services.authenticity import AuthenticityService

        monkeypatch.setattr(
            AuthenticityService,
            "run",
            lambda *args: (_ for _ in ()).throw(RuntimeError("checker unavailable")),
        )
        photo = handoff.photos[0]
        with pytest.raises(RuntimeError, match="checker unavailable"):
            service._route_categories(
                customer_id,
                handoff,
                {photo.photo_id: SimpleNamespace(asset=SimpleNamespace(id=uuid4()))},
                record_job=lambda job_id, phase: service._record_step(
                    customer_id, handoff, "d" * 64, phase, job_id
                ),
            )
        phases = [
            event.payload["phase"]
            for event in session.query(AuditEvent).filter(
                AuditEvent.event_type == "classification_bridge.step"
            )
        ]

    assert phases == ["job_committed", "authenticity_failed"]


def _stage_for_test(tmp_path: Path):  # type: ignore[no-untyped-def]
    from opc_style.services.local_media_stage import LocalMediaStage
    from opc_style.settings import LocalBridgeSettings

    return LocalMediaStage(
        settings=LocalBridgeSettings(
            local_classification_bridge=True,
            local_classification_bridge_nonce="test-bridge-nonce",
            local_classification_bridge_stage_root=tmp_path / "bridge-stage",
            local_classification_bridge_asset_root=tmp_path / "bridge-assets",
        ),
        caller_host="127.0.0.1",
    )


def test_multi_category_with_two_ready_products_reaches_human_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api_session_factory: sessionmaker[Session],
) -> None:
    _active_style_pin(api_session_factory)
    _seed_second_ready_product(api_session_factory)
    body = _jpeg_bytes()
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        real = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        multi = client.post(
            "/v1/local-test/classification-bridge/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        payload = _payload(
            receipt_id=real["receipt_id"], content_sha256=real["content_sha256"]
        )
        payload["photos"][0]["photo_id"] = real["photo_id"]  # type: ignore[index]
        payload["main_photo_id"] = real["photo_id"]
        multi_photo = deepcopy(payload["photos"][0])  # type: ignore[index]
        multi_photo["photo_id"] = multi["photo_id"]
        multi_photo["media_receipt_id"] = multi["receipt_id"]
        multi_photo["automatic_category"] = "multi_product_photo"
        multi_photo["confirmed_category"] = "multi_product_photo"
        payload["photos"].append(multi_photo)  # type: ignore[index]
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(
                ClassificationStyleHandoffV1.model_validate(payload)
            ),
        )

    assert response.status_code == 201
    assert response.json()["status"] == "awaiting_review"
    assert len(response.json()["output_job_ids"]) == 2
