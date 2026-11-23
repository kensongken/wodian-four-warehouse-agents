"""Scene and multi-product orchestration using synthetic artifacts only."""

import base64
import gc
import weakref
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event, Lock, Thread
from time import sleep
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.api.routes_image_jobs import get_image_generator
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.domain.image_generator import (
    DeterministicLocalImageGenerator,
    GeneratedCandidate,
    GenerationRequest,
    ImageGenerationError,
)
from opc_style.domain.image_normalizer import NormalizedImage, NormalizedPayload
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    Base,
    Customer,
    ImageCandidate,
    ImageOutputJob,
    ProductAsset,
    ProductAssetLink,
    ProductAssetPermissionEvent,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
)
from opc_style.providers.openai_images import OpenAIImageGenerator, RealImageBudget
from opc_style.repositories import ProductImageRepository
from opc_style.services import image_jobs as image_jobs_module
from opc_style.services.image_jobs import (
    ImageGenerationOutcomeUncertainError,
    ImageJobService,
    ImageSourceSelection,
)
from opc_style.services.real_photos import (
    AssetContentStoreError,
    DerivativeTarget,
    LocalSyntheticAssetStore,
    UnconfiguredAssetContentStore,
)
from opc_style.settings import RealImageProviderSettings

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'image-jobs-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def content_store(
    tmp_path: Path,
) -> Generator[LocalSyntheticAssetStore, None, None]:
    with LocalSyntheticAssetStore(tmp_path / "synthetic-generated-assets") as store:
        yield store


@pytest.fixture
def generator() -> DeterministicLocalImageGenerator:
    return DeterministicLocalImageGenerator()


@pytest.fixture
def client(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    generator: DeterministicLocalImageGenerator,
) -> Generator[TestClient, None, None]:
    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_asset_content_store] = lambda: content_store
    app.dependency_overrides[get_image_generator] = lambda: generator
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def style_factory(
    api_session_factory: sessionmaker[Session],
) -> Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]]:
    def create_style(
        *,
        customer_id: UUID = CUSTOMER_A_ID,
        main_active: bool = True,
        scene_active: bool = True,
        scene_card_active: bool = True,
    ) -> tuple[StyleCardVersion, SceneStyle, StyleCardVersion]:
        with api_session_factory() as session:
            customer = session.get(Customer, customer_id)
            if customer is None:
                session.add(Customer(id=customer_id, display_name="Synthetic customer"))
                session.flush()
            main_card = StyleCard(
                customer_id=customer_id, kind="main", active=main_active
            )
            session.add(main_card)
            session.flush()
            main_version = StyleCardVersion(
                customer_id=customer_id,
                style_card_id=main_card.id,
                version=1,
                content={"tone": ["synthetic-quiet"]},
            )
            session.add(main_version)
            session.flush()
            scene_id = uuid4()
            scene_card = StyleCard(
                id=scene_id,
                customer_id=customer_id,
                kind="scene",
                active=scene_card_active,
            )
            session.add(scene_card)
            session.flush()
            scene = SceneStyle(
                id=scene_id,
                customer_id=customer_id,
                style_card_id=scene_card.id,
                name=f"Synthetic scene {scene_id}",
                active=scene_active,
                rules={},
            )
            session.add(scene)
            scene_version = StyleCardVersion(
                customer_id=customer_id,
                style_card_id=scene_card.id,
                version=1,
                content={
                    "main_style_card_version_id": str(main_version.id),
                    "name": scene.name,
                    "overrides": {"backgrounds": ["synthetic studio"]},
                    "aspect_ratio": "1:1",
                    "source": "synthetic_test",
                },
            )
            session.add(scene_version)
            session.commit()
            for item in (main_version, scene, scene_version):
                session.refresh(item)
                session.expunge(item)
        return main_version, scene, scene_version

    return create_style


@pytest.fixture
def asset_factory(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    tmp_path: Path,
) -> Callable[..., ProductAsset]:
    def create_asset(
        *,
        customer_id: UUID = CUSTOMER_A_ID,
        product_id: str = "product-001",
        confirmed: bool = True,
        permission: bool | None = True,
    ) -> ProductAsset:
        with api_session_factory() as session:
            customer = session.get(Customer, customer_id)
            if customer is None:
                session.add(Customer(id=customer_id, display_name="Synthetic customer"))
                session.flush()
            source_seed = uuid4()
            source = BytesIO()
            Image.new(
                "RGB",
                (16, 16),
                (source_seed.bytes[0], source_seed.bytes[1], source_seed.bytes[2]),
            ).save(source, format="JPEG")
            payload = source.getvalue()
            asset = ProductAsset(
                customer_id=customer_id,
                product_id=product_id,
                photo_id=f"photo-{uuid4()}",
                identity="original_real_photo",
                confirmed=confirmed,
                source_type="synthetic_metadata",
                quality_flags=["synthetic_fixture"],
                occurred_at=NOW,
                content_sha256=sha256(payload).hexdigest(),
                mime_type="image/jpeg",
                width=1200,
                height=1200,
                storage_path=None,
            )
            session.add(asset)
            session.flush()
            if permission is not None:
                session.add(
                    ProductAssetPermissionEvent(
                        customer_id=customer_id,
                        product_asset_id=asset.id,
                        transform_permission=permission,
                        occurred_at=NOW,
                        provenance="synthetic_test",
                    )
                )
            session.commit()
            session.refresh(asset)
            session.expunge(asset)
        source_path = tmp_path / f"source-{asset.id}.jpg"
        source_path.write_bytes(payload)
        content_store.register_original(customer_id, asset.id, source_path)
        return asset

    return create_asset


def _request(
    assets: list[ProductAsset],
    scene: SceneStyle,
    scene_version: StyleCardVersion,
    *,
    idempotency_key: str = "generate-request-001",
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sources": [
            {"product_id": asset.product_id, "asset_id": str(asset.id)}
            for asset in assets
        ],
        "scene_style_id": str(scene.id),
        "style_card_version_id": str(scene_version.id),
        "output_ratio": "1:1",
        "idempotency_key": idempotency_key,
    }
    payload.update(overrides)
    return payload


def _exact_generator_using(
    delegate: DeterministicLocalImageGenerator,
) -> DeterministicLocalImageGenerator:
    generator = DeterministicLocalImageGenerator()
    generator.generate = delegate.generate  # type: ignore[method-assign]
    return generator


def gate_concurrent_missing_job_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make two requests observe the same missing idempotency-key state."""
    from threading import Barrier

    barrier = Barrier(2)
    original_get = ProductImageRepository.get_output_job_by_idempotency_key

    def gated_get(
        repository: ProductImageRepository,
        customer_id: UUID,
        idempotency_key: str,
    ) -> ImageOutputJob | None:
        job = original_get(repository, customer_id, idempotency_key)
        if job is None:
            barrier.wait(timeout=5)
        return job

    monkeypatch.setattr(
        ProductImageRepository, "get_output_job_by_idempotency_key", gated_get
    )


def test_deterministic_generator_returns_three_in_memory_synthetic_payloads() -> None:
    request = GenerationRequest(
        output_type="scene",
        product_ids=("product-001",),
        asset_ids=(UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),),
        style_card_version_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        main_style_card_version_id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
        scene_style_id=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
        output_ratio="1:1",
    )
    generator = DeterministicLocalImageGenerator()

    first = generator.generate(request)
    second = generator.generate(request)

    assert len(first) == 3
    assert first == second
    assert generator.adapter_version == "deterministic-local-image-v1"
    assert generator.generation_mode == "synthetic_test_only"
    for sequence_number, candidate in enumerate(first, start=1):
        assert candidate.content_sha256 == sha256(candidate.content).hexdigest()
        assert candidate.mime_type == "image/jpeg"
        assert candidate.metadata["adapter_version"] == generator.adapter_version
        assert candidate.metadata["generation_mode"] == "synthetic_test_only"
        assert candidate.metadata["synthetic_test_only"] is True
        assert candidate.metadata["publishable"] is False
        assert candidate.metadata["sequence_number"] == sequence_number
        assert not hasattr(candidate, "output_path")
        with Image.open(BytesIO(candidate.content)) as artifact:
            assert artifact.format == "JPEG"
            assert artifact.size == (512, 512)


def test_generator_receives_only_store_verified_authorized_source_bytes(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    content_store: LocalSyntheticAssetStore,
    generator: DeterministicLocalImageGenerator,
) -> None:
    main_version, scene, scene_version = style_factory()
    del main_version
    asset = asset_factory()
    content = content_store.read_original(CUSTOMER_A_ID, asset.id, asset.content_sha256)
    original_generate = generator.generate
    captured: list[GenerationRequest] = []

    def capture(request: GenerationRequest) -> list[GeneratedCandidate]:
        captured.append(request)
        return original_generate(request)

    generator.requires_source_content = True  # type: ignore[attr-defined]
    generator.generate = capture  # type: ignore[method-assign]

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )

    assert response.status_code == 201
    assert len(captured) == 1
    assert len(captured[0].sources) == 1
    source = captured[0].sources[0]
    assert source.asset_id == asset.id
    assert source.product_id == asset.product_id
    assert source.content == content
    assert source.content_sha256 == sha256(content).hexdigest()
    assert source.mime_type == asset.mime_type


@pytest.mark.parametrize("positive_budget", ["0.50", "1.00", "7.50"])
def test_legacy_product_style_endpoint_rejects_real_provider_without_identity_card(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    positive_budget: str,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    output = BytesIO()
    Image.new("RGB", (32, 32), (20, 30, 40)).save(output, format="JPEG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    provider_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(
            200,
            json={"data": [{"b64_json": encoded} for _ in range(3)]},
        )

    real_generator = OpenAIImageGenerator(
        RealImageProviderSettings(
            enabled=True,
            api_key="test-secret-value",
            max_run_usd=positive_budget,
            _env_file=None,
        ),
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        budget=RealImageBudget(Decimal(positive_budget)),
    )
    client.app.dependency_overrides[get_image_generator] = lambda: real_generator

    response = client.post(
        "/v1/image-outputs/product-style",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="real-provider"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == (
        "confirmed_product_identity_card_required"
    )
    assert provider_calls == 0


def test_product_style_endpoint_fails_closed_while_real_provider_is_disabled(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/product-style",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="product-style"),
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "real_image_provider_disabled", "retryable": False}
    }


def test_scene_job_creates_exactly_three_traceable_immutable_candidates(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    main_version, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["output_type"] == "scene"
    assert body["status"] == "machine_check"
    assert body["adapter_version"] == "deterministic-local-image-v1"
    assert body["generation_mode"] == "synthetic_test_only"
    assert body["synthetic_test_only"] is True
    assert body["publishable"] is False
    assert body["style_card_version_id"] == str(scene_version.id)
    assert body["main_style_card_version_id"] == str(main_version.id)
    assert body["scene_style_id"] == str(scene.id)
    assert body["source_product_ids"] == [asset.product_id]
    assert body["source_asset_ids"] == [str(asset.id)]
    assert len(body["candidates"]) == 3
    assert [candidate["sequence_number"] for candidate in body["candidates"]] == [
        1,
        2,
        3,
    ]
    for candidate in body["candidates"]:
        assert candidate["identity"] == "synthetic_style_image_candidate"
        assert candidate["candidate_state"] == "pending_authenticity_check"
        assert candidate["adapter_version"] == "deterministic-local-image-v1"
        assert candidate["generation_mode"] == "synthetic_test_only"
        assert candidate["synthetic_test_only"] is True
        assert candidate["publishable"] is False
        assert candidate["source_product_ids"] == [asset.product_id]
        assert candidate["source_asset_ids"] == [str(asset.id)]
        assert candidate["output_job_id"] == body["id"]
        assert candidate["style_card_version_id"] == str(scene_version.id)
        assert candidate["main_style_card_version_id"] == str(main_version.id)
        assert candidate["scene_style_id"] == str(scene.id)
        assert "artifact_path" not in candidate

    with api_session_factory() as session:
        job = session.get(ImageOutputJob, UUID(body["id"]))
        assert job is not None
        assert job.status == "machine_check"
        assert job.style_card_version_id == scene_version.id
        assert job.scene_style_id == scene.id
        assert session.query(ProductAssetLink).count() == 1
        assert session.query(ImageCandidate).count() == 3
        event_types = [event.event_type for event in session.query(AuditEvent).all()]
        assert event_types == [
            "image_output.generation_requested",
            "image_output.generation_completed",
        ]


def test_multi_product_job_traces_every_distinct_selected_product_and_asset(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    main_version, scene, scene_version = style_factory()
    assets = [asset_factory(product_id=f"product-{index}") for index in range(2, 7)]

    options = client.get(
        "/local/style-suite/api/multi-product/options", headers=CUSTOMER_A
    )
    assert options.status_code == 200
    assert {item["product_id"] for item in options.json()} >= {
        asset.product_id for asset in assets
    }

    response = client.post(
        "/v1/image-outputs/multi-product",
        headers=CUSTOMER_A,
        json=_request(assets, scene, scene_version, idempotency_key="multi-five"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["output_type"] == "multi_product"
    assert body["source_product_ids"] == [asset.product_id for asset in assets]
    assert body["source_asset_ids"] == [str(asset.id) for asset in assets]
    assert body["main_style_card_version_id"] == str(main_version.id)
    assert len(body["candidates"]) == 3
    for candidate in body["candidates"]:
        assert candidate["source_product_ids"] == body["source_product_ids"]
        assert candidate["source_asset_ids"] == body["source_asset_ids"]
        content = client.get(
            f"/local/style-suite/api/multi-product/jobs/{body['id']}"
            f"/candidates/{candidate['id']}/content",
            headers=CUSTOMER_A,
        )
        assert content.status_code == 200
        assert content.headers["x-content-sha256"] == candidate["content_sha256"]
        assert client.get(
            f"/local/style-suite/api/multi-product/jobs/{body['id']}"
            f"/candidates/{candidate['id']}/content",
            headers=CUSTOMER_B,
        ).status_code == 404
    gate = client.post(
        f"/v1/image-outputs/{body['id']}/authenticity-checks", headers=CUSTOMER_A
    )
    assert gate.status_code == 201
    assert gate.json()["status"] == "awaiting_customer_review"
    approved = client.post(
        f"/v1/image-outputs/{body['id']}/candidates/"
        f"{body['candidates'][0]['id']}/review",
        headers=CUSTOMER_A,
        json={
            "decision": "approved",
            "idempotency_key": "multi-five-review",
            "fidelity_warning_acknowledged": False,
        },
    )
    assert approved.status_code == 201
    assert approved.json()["customer_approved"] is True
    assert approved.json()["publishable"] is False
    assert approved.json()["source_product_ids"] == [
        asset.product_id for asset in assets
    ]
    with api_session_factory() as session:
        links = (
            session.query(ProductAssetLink).order_by(ProductAssetLink.position).all()
        )
        assert [link.product_asset_id for link in links] == [
            asset.id for asset in assets
        ]


def test_wear_job_creates_three_review_gated_candidates_for_one_product(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
) -> None:
    main_version, scene, scene_version = style_factory()
    asset = asset_factory(product_id="product-wear-001")

    response = client.post(
        "/v1/image-outputs/wear",
        headers=CUSTOMER_A,
        json=_request(
            [asset],
            scene,
            scene_version,
            idempotency_key="wear-three",
            wear_presentation={
                "position": "wrist",
                "skin_tone": "medium",
                "gender_presentation": "neutral",
                "age_presentation": "adult",
                "styling": "commercial",
            },
        ),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["output_type"] == "wear"
    assert body["publishable"] is False
    assert body["status"] == "machine_check"
    assert body["source_product_ids"] == [asset.product_id]
    assert body["main_style_card_version_id"] == str(main_version.id)
    assert body["presentation_origin"] == "system_virtual_model"
    assert body["real_wear_photo"] is False
    assert body["wear_presentation"]["position"] == "wrist"
    assert len(body["candidates"]) == 3
    assert all(candidate["publishable"] is False for candidate in body["candidates"])
    for candidate in body["candidates"]:
        content = client.get(
            f"/local/style-suite/api/reviewable-image-jobs/{body['id']}"
            f"/candidates/{candidate['id']}/content",
            headers=CUSTOMER_A,
        )
        assert content.status_code == 200
        assert content.headers["x-content-sha256"] == candidate["content_sha256"]
        assert client.get(
            f"/local/style-suite/api/reviewable-image-jobs/{body['id']}"
            f"/candidates/{candidate['id']}/content",
            headers=CUSTOMER_B,
        ).status_code == 404
    gate = client.post(
        f"/v1/image-outputs/{body['id']}/authenticity-checks", headers=CUSTOMER_A
    )
    assert gate.status_code == 201
    assert gate.json()["status"] == "awaiting_customer_review"
    approved = client.post(
        f"/v1/image-outputs/{body['id']}/candidates/"
        f"{body['candidates'][0]['id']}/review",
        headers=CUSTOMER_A,
        json={
            "decision": "approved",
            "idempotency_key": "wear-three-review",
            "fidelity_warning_acknowledged": False,
        },
    )
    assert approved.status_code == 201
    assert approved.json()["customer_approved"] is True
    assert approved.json()["publishable"] is False


@pytest.mark.parametrize(
    ("endpoint", "product_ids", "expected_code"),
    [
        ("scene", ["one", "two"], "scene_requires_one_product"),
        ("wear", ["one", "two"], "wear_requires_one_product"),
        (
            "multi-product",
            ["one"],
            "multi_product_requires_2_to_5_distinct_products",
        ),
        (
            "multi-product",
            ["one", "two", "three", "four", "five", "six"],
            "multi_product_requires_2_to_5_distinct_products",
        ),
        (
            "multi-product",
            ["duplicate", "duplicate"],
            "multi_product_requires_2_to_5_distinct_products",
        ),
    ],
)
def test_product_cardinality_is_enforced_before_job_mutation(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    endpoint: str,
    product_ids: list[str],
    expected_code: str,
) -> None:
    _, scene, scene_version = style_factory()
    assets = [asset_factory(product_id=product_id) for product_id in product_ids]

    response = client.post(
        f"/v1/image-outputs/{endpoint}",
        headers=CUSTOMER_A,
        json=_request(assets, scene, scene_version),
    )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": expected_code, "retryable": False}}
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0
        assert session.query(ProductAssetLink).count() == 0
        assert session.query(ImageCandidate).count() == 0
        assert session.query(AuditEvent).count() == 0


@pytest.mark.parametrize(
    ("asset_options", "expected_code"),
    [
        ({"confirmed": False}, "organizer_confirmation_missing"),
        ({"permission": None}, "transform_permission_unknown"),
        ({"permission": False}, "transform_permission_denied"),
    ],
)
def test_every_source_must_be_confirmed_and_currently_permissioned(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    asset_options: dict[str, object],
    expected_code: str,
) -> None:
    _, scene, scene_version = style_factory()
    ready = asset_factory(product_id="ready-product")
    blocked = asset_factory(product_id="blocked-product", **asset_options)

    response = client.post(
        "/v1/image-outputs/multi-product",
        headers=CUSTOMER_A,
        json=_request([ready, blocked], scene, scene_version),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0
        assert session.query(ImageCandidate).count() == 0


def test_latest_permission_event_controls_generation(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory(permission=True)
    with api_session_factory() as session:
        session.add(
            ProductAssetPermissionEvent(
                customer_id=CUSTOMER_A_ID,
                product_asset_id=asset.id,
                transform_permission=False,
                occurred_at=NOW + timedelta(seconds=1),
                provenance="synthetic_withdrawal",
            )
        )
        session.commit()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "transform_permission_denied"
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0


def test_foreign_or_mismatched_source_and_style_identities_are_hidden(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    _, foreign_scene, foreign_version = style_factory(customer_id=CUSTOMER_B_ID)
    foreign_asset = asset_factory(customer_id=CUSTOMER_B_ID)
    requests = [
        _request(
            [foreign_asset], scene, scene_version, idempotency_key="foreign-asset"
        ),
        _request(
            [asset],
            scene,
            scene_version,
            idempotency_key="product-mismatch",
            sources=[{"product_id": "not-the-product", "asset_id": str(asset.id)}],
        ),
        _request(
            [asset], foreign_scene, foreign_version, idempotency_key="foreign-style"
        ),
        _request(
            [asset],
            scene,
            foreign_version,
            idempotency_key="mismatched-version",
        ),
    ]

    for payload in requests:
        response = client.post(
            "/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload
        )
        assert response.status_code == 404, response.text

    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0
        assert session.query(ImageCandidate).count() == 0


@pytest.mark.parametrize(
    "style_options",
    [
        {"main_active": False},
        {"scene_active": False},
        {"scene_card_active": False},
    ],
)
def test_inactive_main_or_scene_style_blocks_generation(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    style_options: dict[str, bool],
) -> None:
    _, scene, scene_version = style_factory(**style_options)
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "style_or_scene_inactive"
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0


def test_idempotency_replays_same_job_and_rejects_different_facts(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    _, scene, scene_version = style_factory()
    first_asset = asset_factory(product_id="product-one")
    second_asset = asset_factory(product_id="product-two")
    payload = _request([first_asset], scene, scene_version)

    created = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)
    replay = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)
    conflict = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([second_asset], scene, scene_version),
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == created.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "image_output_idempotency_conflict"
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 1
        assert session.query(ImageCandidate).count() == 3


class FailingGenerator(DeterministicLocalImageGenerator):
    """A provider double that fails before returning any in-memory batch."""

    adapter_version = "deterministic-local-image-v1"
    generation_mode = "synthetic_test_only"
    publishable = False

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        del request
        raise ImageGenerationError("synthetic_generator_unavailable")


class ShortBatchGenerator(DeterministicLocalImageGenerator):
    """A provider double violating the exactly-three response contract."""

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        return super().generate(request)[:2]


class MislabeledGenerator(DeterministicLocalImageGenerator):
    """A provider double that lies about which adapter made the payload."""

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        generated = super().generate(request)
        bad_metadata = dict(generated[0].metadata)
        bad_metadata["adapter_version"] = "pretend-production-provider-v9"
        generated[0] = replace(generated[0], metadata=bad_metadata)
        return generated


class GarbageJpegGenerator(DeterministicLocalImageGenerator):
    """Return self-consistent metadata around bytes that are not a JPEG."""

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        generated = super().generate(request)
        garbage = b"not-really-a-jpeg"
        return [
            replace(
                candidate,
                content=garbage,
                content_sha256=sha256(garbage).hexdigest(),
            )
            for candidate in generated
        ]


class LabelOnlyGenerator:
    adapter_version = "deterministic-local-image-v1"
    generation_mode = "synthetic_test_only"
    publishable = False

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        return DeterministicLocalImageGenerator().generate(request)


class RegisteredGeneratorSubclass(DeterministicLocalImageGenerator):
    """A subclass must not inherit production adapter trust implicitly."""


@pytest.mark.parametrize(
    "replacement",
    [
        _exact_generator_using(FailingGenerator()),
        _exact_generator_using(ShortBatchGenerator()),
        _exact_generator_using(MislabeledGenerator()),
        _exact_generator_using(GarbageJpegGenerator()),
    ],
)
def test_generator_failure_records_failed_job_without_partial_candidate_or_artifact(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    replacement: object,
) -> None:
    client.app.dependency_overrides[get_image_generator] = lambda: replacement
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "generation_failed"
    assert body["candidates"] == []
    assert body["failure_reason"] in {
        "synthetic_generator_unavailable",
        "generator_must_return_exactly_three_candidates",
        "invalid_generated_candidate",
    }
    assert content_store.list_derivatives(CUSTOMER_A_ID, UUID(body["id"])) == ()
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 0
        assert [event.event_type for event in session.query(AuditEvent).all()] == [
            "image_output.generation_requested",
            "image_output.generation_failed",
        ]


def test_matching_labels_do_not_make_an_untrusted_generator_executable(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    client.app.dependency_overrides[get_image_generator] = LabelOnlyGenerator
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_image_generator"
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0


def test_generator_subclass_is_rejected_even_when_base_constructor_registered_it(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    client.app.dependency_overrides[get_image_generator] = RegisteredGeneratorSubclass
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_image_generator"
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0


class FailSecondPublishStore:
    """Delegate to the real store but fail after the first publication."""

    def __init__(self, delegate: LocalSyntheticAssetStore) -> None:
        self._delegate = delegate

    @property
    def store_identity(self) -> str:
        return self._delegate.store_identity

    @property
    def store_namespace(self) -> str:
        return self._delegate.store_namespace

    def reserve_derivative(
        self, customer_id: UUID, job_id: UUID, sequence_number: int
    ) -> DerivativeTarget:
        return self._delegate.reserve_derivative(customer_id, job_id, sequence_number)

    def create_derivative(
        self, target: DerivativeTarget, payload: NormalizedPayload
    ) -> NormalizedImage:
        if target.sequence_number == 2:
            raise AssetContentStoreError("synthetic_second_publish_failed")
        return self._delegate.create_derivative(target, payload)

    def cleanup_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[str, ...]:
        return self._delegate.cleanup_derivatives(customer_id, job_id)

    def issue_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
    ) -> str:
        return self._delegate.issue_cleanup_capability(
            customer_id, job_id, expected_sequences
        )

    def verify_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> bool:
        return self._delegate.verify_cleanup_capability(
            customer_id, job_id, expected_sequences, capability
        )

    def cleanup_scoped_derivatives(
        self, customer_id: UUID, job_id: UUID, capability: str
    ) -> tuple[str, ...]:
        if not self.verify_cleanup_capability(
            customer_id, job_id, (1, 2, 3), capability
        ):
            return ("asset_store_cleanup_capability_invalid",)
        return self.cleanup_derivatives(customer_id, job_id)

    def cleanup_scope_state(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> str:
        return self._delegate.cleanup_scope_state(
            customer_id, job_id, expected_sequences, capability
        )

    def verify_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bool:
        return self._delegate.verify_derivative(
            customer_id, job_id, sequence_number, expected_sha256
        )


def test_store_failure_rolls_back_every_published_artifact_and_candidate(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    client.app.dependency_overrides[get_asset_content_store] = lambda: (
        FailSecondPublishStore(content_store)
    )
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "generation_failed"
    assert body["failure_reason"] == "synthetic_second_publish_failed"
    assert body["candidates"] == []
    assert content_store.list_derivatives(CUSTOMER_A_ID, UUID(body["id"])) == ()
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 0


def test_get_is_customer_scoped_and_keeps_real_photo_status_compatible(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    created = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version),
    )
    job_id = created.json()["id"]

    own = client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_A)
    foreign = client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_B)
    missing = client.get(f"/v1/image-outputs/{uuid4()}", headers=CUSTOMER_A)

    assert own.status_code == 200
    assert own.json() == created.json()
    assert foreign.status_code == 404
    assert missing.status_code == 404


def test_concurrent_identical_idempotency_requests_safely_replay_one_job(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    payload = _request(
        [asset], scene, scene_version, idempotency_key="concurrent-identical"
    )
    initial_lock_count = len(image_jobs_module._LOCAL_JOB_LOCKS)
    initial_store_count = len(image_jobs_module._LOCAL_JOB_STORES)
    gate_concurrent_missing_job_reads(monkeypatch)

    def submit() -> tuple[int, dict[str, Any]]:
        response = client.post(
            "/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))

    assert sorted(status for status, _ in results) == [200, 201]
    assert len({body["id"] for _, body in results}) == 1
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 1
        assert session.query(ProductAssetLink).count() == 1
        assert session.query(ImageCandidate).count() == 3
    assert len(image_jobs_module._LOCAL_JOB_LOCKS) == initial_lock_count
    assert len(image_jobs_module._LOCAL_JOB_STORES) == initial_store_count + 1


def test_concurrent_conflicting_idempotency_requests_return_409_not_500(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, scene, scene_version = style_factory()
    assets = [asset_factory(product_id=f"concurrent-{index}") for index in range(2)]
    payloads = [
        _request([asset], scene, scene_version, idempotency_key="concurrent-conflict")
        for asset in assets
    ]
    initial_lock_count = len(image_jobs_module._LOCAL_JOB_LOCKS)
    initial_store_count = len(image_jobs_module._LOCAL_JOB_STORES)
    gate_concurrent_missing_job_reads(monkeypatch)

    def submit(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        response = client.post(
            "/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, payloads))

    assert sorted(status for status, _ in results) == [201, 409]
    conflict = next(body for status, body in results if status == 409)
    assert conflict == {
        "error": {"code": "image_output_idempotency_conflict", "retryable": False}
    }
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 1
        assert session.query(ProductAssetLink).count() == 1
        assert session.query(ImageCandidate).count() == 3
    assert len(image_jobs_module._LOCAL_JOB_LOCKS) == initial_lock_count
    assert len(image_jobs_module._LOCAL_JOB_STORES) == initial_store_count + 1


def test_candidate_commit_ack_loss_reloads_success_without_cleanup_or_downgrade(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    main_version, scene, scene_version = style_factory()
    asset = asset_factory()
    with api_session_factory() as session:
        real_commit = session.commit
        commit_calls = 0

        def commit_with_lost_ack() -> None:
            nonlocal commit_calls
            commit_calls += 1
            real_commit()
            if commit_calls == 3:
                raise RuntimeError("synthetic commit acknowledgement lost")

        session.commit = commit_with_lost_ack  # type: ignore[method-assign]
        result = ImageJobService(
            session, content_store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=(
                ImageSourceSelection(product_id=asset.product_id, asset_id=asset.id),
            ),
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key="commit-ack-loss",
        )

    assert result.job.status == "machine_check"
    assert result.job.failure_reason is None
    assert result.job.style_card_version_id == scene_version.id
    assert result.job.request_metadata["main_style_card_version_id"] == str(
        main_version.id
    )
    assert len(result.candidates) == 3
    assert len(content_store.list_derivatives(CUSTOMER_A_ID, result.job.id)) == 3
    with api_session_factory() as session:
        job = session.get(ImageOutputJob, result.job.id)
        assert job is not None and job.status == "machine_check"
        assert session.query(ImageCandidate).count() == 3
        event_types = [event.event_type for event in session.query(AuditEvent).all()]
        assert "image_output.generation_completed" in event_types
        assert "image_output.generation_failed" not in event_types
        assert "image_output.cleanup_pending" not in event_types


def test_failed_intake_releases_process_lock_and_store_registration(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    initial_lock_count = len(image_jobs_module._LOCAL_JOB_LOCKS)
    initial_store_count = len(image_jobs_module._LOCAL_JOB_STORES)
    with api_session_factory() as session:

        def fail_commit() -> None:
            raise RuntimeError("synthetic intake commit failed")

        session.commit = fail_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="synthetic intake commit failed"):
            ImageJobService(
                session, content_store, DeterministicLocalImageGenerator()
            ).create(
                CUSTOMER_A_ID,
                output_type="scene",
                sources=(
                    ImageSourceSelection(
                        product_id=asset.product_id, asset_id=asset.id
                    ),
                ),
                scene_style_id=scene.id,
                style_card_version_id=scene_version.id,
                output_ratio="1:1",
                idempotency_key="failed-intake-registry",
            )

    assert len(image_jobs_module._LOCAL_JOB_LOCKS) == initial_lock_count
    assert len(image_jobs_module._LOCAL_JOB_STORES) == initial_store_count


def test_candidate_commit_ack_loss_never_returns_success_after_artifact_loss(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    with api_session_factory() as session:
        real_commit = session.commit
        commit_calls = 0

        def commit_then_lose_artifacts_and_ack() -> None:
            nonlocal commit_calls
            commit_calls += 1
            real_commit()
            if commit_calls == 3:
                job = session.query(ImageOutputJob).one()
                assert content_store.cleanup_derivatives(CUSTOMER_A_ID, job.id) == ()
                raise RuntimeError(
                    "candidate commit acknowledgement and artifacts lost"
                )

        session.commit = commit_then_lose_artifacts_and_ack  # type: ignore[method-assign]
        with pytest.raises(ImageGenerationOutcomeUncertainError):
            ImageJobService(
                session, content_store, DeterministicLocalImageGenerator()
            ).create(
                CUSTOMER_A_ID,
                output_type="scene",
                sources=(
                    ImageSourceSelection(
                        product_id=asset.product_id, asset_id=asset.id
                    ),
                ),
                scene_style_id=scene.id,
                style_card_version_id=scene_version.id,
                output_ratio="1:1",
                idempotency_key="candidate-ack-artifact-loss",
            )

    with api_session_factory() as session:
        job = session.query(ImageOutputJob).one()
        assert job.status == "machine_check"
        assert session.query(ImageCandidate).count() == 3


@pytest.mark.parametrize("lost_ack_commit_number", [1, 2])
def test_intake_or_claim_commit_ack_loss_still_completes_generation(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    lost_ack_commit_number: int,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    with api_session_factory() as session:
        real_commit = session.commit
        commit_calls = 0

        def commit_with_lost_ack() -> None:
            nonlocal commit_calls
            commit_calls += 1
            real_commit()
            if commit_calls == lost_ack_commit_number:
                raise RuntimeError("synthetic commit acknowledgement lost")

        session.commit = commit_with_lost_ack  # type: ignore[method-assign]
        result = ImageJobService(
            session, content_store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=(
                ImageSourceSelection(product_id=asset.product_id, asset_id=asset.id),
            ),
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key=f"early-ack-loss-{lost_ack_commit_number}",
        )

    assert result.job.status == "machine_check"
    assert len(result.candidates) == 3
    with api_session_factory() as session:
        job = session.get(ImageOutputJob, result.job.id)
        assert job is not None and job.status == "machine_check"
        assert session.query(ImageCandidate).count() == 3


class CrashOnceGenerator(DeterministicLocalImageGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.crash = True

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        if self.crash:
            self.crash = False
            raise SystemExit("synthetic worker crash")
        return super().generate(request)


def _leave_generating_job(
    *,
    scene: SceneStyle,
    scene_version: StyleCardVersion,
    asset: ProductAsset,
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    idempotency_key: str,
) -> None:
    with api_session_factory() as session, pytest.raises(SystemExit):
        ImageJobService(
            session,
            content_store,
            _exact_generator_using(CrashOnceGenerator()),
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=(
                ImageSourceSelection(product_id=asset.product_id, asset_id=asset.id),
            ),
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key=idempotency_key,
        )


def test_finished_crashed_worker_can_be_recovered_by_local_replay(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    payload = _request([asset], scene, scene_version, idempotency_key="active-claim")
    _leave_generating_job(
        scene=scene,
        scene_version=scene_version,
        asset=asset,
        api_session_factory=api_session_factory,
        content_store=content_store,
        idempotency_key="active-claim",
    )

    response = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "machine_check"


def test_interrupted_local_generation_is_recovered_after_job_lock_is_released(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    payload = _request([asset], scene, scene_version, idempotency_key="expired-claim")
    _leave_generating_job(
        scene=scene,
        scene_version=scene_version,
        asset=asset,
        api_session_factory=api_session_factory,
        content_store=content_store,
        idempotency_key="expired-claim",
    )
    response = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "machine_check"
    assert len(response.json()["candidates"]) == 3


class StealGenerationClaimGenerator(DeterministicLocalImageGenerator):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        super().__init__()
        self._session_factory = session_factory

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        with self._session_factory() as session:
            job = session.query(ImageOutputJob).one()
            job.generation_claim_token = str(uuid4())
            job.updated_at = datetime.now(UTC)
            session.commit()
        return super().generate(request)


class RaiseAfterStealingClaimGenerator(StealGenerationClaimGenerator):
    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        super().generate(request)
        raise ImageGenerationError("old_worker_failed_after_claim_loss")


def test_worker_that_loses_generation_claim_is_fenced_before_publication(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    client.app.dependency_overrides[get_image_generator] = lambda: (
        _exact_generator_using(StealGenerationClaimGenerator(api_session_factory))
    )
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="stolen-claim"),
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "image_generation_in_progress", "retryable": True}
    }
    with api_session_factory() as session:
        job = session.query(ImageOutputJob).one()
        assert job.status == "generating"
        assert session.query(ImageCandidate).count() == 0
        assert content_store.list_derivatives(CUSTOMER_A_ID, job.id) == ()


def test_worker_that_loses_claim_cannot_cleanup_or_downgrade_new_owner(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    client.app.dependency_overrides[get_image_generator] = lambda: (
        _exact_generator_using(RaiseAfterStealingClaimGenerator(api_session_factory))
    )
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="stolen-failure"),
    )

    assert response.status_code == 503
    with api_session_factory() as session:
        job = session.query(ImageOutputJob).one()
        assert job.status == "generating"
        assert job.generation_claim_token is not None
        assert job.failure_reason is None
        assert content_store.list_derivatives(CUSTOMER_A_ID, job.id) == ()


class BlockingGenerator(DeterministicLocalImageGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self._lock = Lock()
        self.calls = 0

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        with self._lock:
            self.calls += 1
            call_number = self.calls
        if call_number == 1:
            self.entered.set()
            assert self.release.wait(timeout=3)
        return super().generate(request)


def test_local_job_lock_prevents_double_execution(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
) -> None:
    generator = BlockingGenerator()
    exact_generator = _exact_generator_using(generator)
    client.app.dependency_overrides[get_image_generator] = lambda: exact_generator
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    payload = _request([asset], scene, scene_version, idempotency_key="short-lease")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            client.post, "/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload
        )
        assert generator.entered.wait(timeout=2)
        sleep(0.25)
        second_future = executor.submit(
            client.post, "/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload
        )
        sleep(0.2)
        generator.release.set()
        first = first_future.result(timeout=3)
        second = second_future.result(timeout=6)

    assert first.status_code == 201
    assert first.json()["status"] == "machine_check"
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert generator.calls == 1


def test_local_executor_never_starts_background_heartbeat_threads(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    heartbeat_starts: list[str] = []
    real_start = Thread.start

    def record_start(thread: Any) -> None:
        target = getattr(thread, "_target", None)
        if getattr(target, "__name__", "") == "heartbeat":
            heartbeat_starts.append(thread.name)
        real_start(thread)

    monkeypatch.setattr(Thread, "start", record_start)
    with api_session_factory() as session:
        result = ImageJobService(
            session, content_store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=(
                ImageSourceSelection(product_id=asset.product_id, asset_id=asset.id),
            ),
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key="no-background-heartbeat",
        )

    assert result.job.status == "machine_check"
    assert heartbeat_starts == []


def test_process_lock_registry_is_reclaimed_while_machine_check_keeps_weak_store_proof(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    initial_lock_count = len(image_jobs_module._LOCAL_JOB_LOCKS)
    initial_store_count = len(image_jobs_module._LOCAL_JOB_STORES)
    initial_store_jobs = set(image_jobs_module._LOCAL_JOB_STORES)

    for index in range(100):
        with api_session_factory() as session:
            result = ImageJobService(
                session, content_store, DeterministicLocalImageGenerator()
            ).create(
                CUSTOMER_A_ID,
                output_type="scene",
                sources=(
                    ImageSourceSelection(
                        product_id=asset.product_id, asset_id=asset.id
                    ),
                ),
                scene_style_id=scene.id,
                style_card_version_id=scene_version.id,
                output_ratio="1:1",
                idempotency_key=f"registry-lifecycle-{index}",
            )
        assert result.job.status == "machine_check"

    assert len(image_jobs_module._LOCAL_JOB_LOCKS) == initial_lock_count
    assert len(image_jobs_module._LOCAL_JOB_STORES) == initial_store_count + 100
    assert all(
        image_jobs_module._LOCAL_JOB_STORES[job_id] is content_store
        for job_id in set(image_jobs_module._LOCAL_JOB_STORES) - initial_store_jobs
    )


def test_completed_job_registry_does_not_prevent_store_close_and_gc(
    tmp_path: Path,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    store = LocalSyntheticAssetStore(tmp_path / "gc-store")
    reference = weakref.ref(store)
    with api_session_factory() as session:
        service = ImageJobService(session, store, DeterministicLocalImageGenerator())
        result = service.create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=(
                ImageSourceSelection(product_id=asset.product_id, asset_id=asset.id),
            ),
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key="store-gc",
        )
    assert result.job.status == "machine_check"
    store.close()
    del service, result, store
    gc.collect()

    assert reference() is None


def test_waiting_lock_users_prevent_registry_aba_and_last_user_removes_entry() -> None:
    job_id = uuid4()
    owner_entered = Event()
    release_owner = Event()
    waiter_started = Event()
    waiter_entered = Event()
    release_waiter = Event()
    late_entered = Event()
    identities: list[int] = []

    def registered_lock_identity() -> int:
        entry = image_jobs_module._LOCAL_JOB_LOCKS[job_id]
        return id(getattr(entry, "lock", entry))

    def owner() -> None:
        with image_jobs_module._local_job_lock(job_id):
            identities.append(registered_lock_identity())
            owner_entered.set()
            assert release_owner.wait(timeout=3)

    def waiter() -> None:
        waiter_started.set()
        with image_jobs_module._local_job_lock(job_id):
            identities.append(registered_lock_identity())
            waiter_entered.set()
            assert release_waiter.wait(timeout=3)

    def late_waiter() -> None:
        with image_jobs_module._local_job_lock(job_id):
            identities.append(registered_lock_identity())
            late_entered.set()

    owner_thread = Thread(target=owner)
    waiter_thread = Thread(target=waiter)
    owner_thread.start()
    assert owner_entered.wait(timeout=2)
    waiter_thread.start()
    assert waiter_started.wait(timeout=2)
    sleep(0.05)
    release_owner.set()
    assert waiter_entered.wait(timeout=2)
    late_thread = Thread(target=late_waiter)
    late_thread.start()
    sleep(0.05)
    release_waiter.set()
    assert late_entered.wait(timeout=2)
    owner_thread.join(timeout=2)
    waiter_thread.join(timeout=2)
    late_thread.join(timeout=2)

    registry_entry_remained = job_id in image_jobs_module._LOCAL_JOB_LOCKS
    image_jobs_module._LOCAL_JOB_LOCKS.pop(job_id, None)
    assert len(set(identities)) == 1
    assert registry_entry_remained is False


class FailPublishAndCleanupOnceStore(FailSecondPublishStore):
    """Leave the first artifact behind once, then permit replay cleanup."""

    def __init__(self, delegate: LocalSyntheticAssetStore) -> None:
        super().__init__(delegate)
        self.cleanup_attempts = 0

    def cleanup_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[str, ...]:
        self.cleanup_attempts += 1
        if self.cleanup_attempts == 1:
            return ("synthetic_cleanup_blocked",)
        return self._delegate.cleanup_derivatives(customer_id, job_id)


class CountCleanupStore(FailSecondPublishStore):
    def __init__(self, delegate: LocalSyntheticAssetStore) -> None:
        super().__init__(delegate)
        self.cleanup_attempts = 0

    def cleanup_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[str, ...]:
        self.cleanup_attempts += 1
        return self._delegate.cleanup_derivatives(customer_id, job_id)


def test_failure_commit_ack_loss_reloads_persisted_result_without_second_cleanup(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    store = CountCleanupStore(content_store)
    with api_session_factory() as session:
        real_commit = session.commit
        commit_calls = 0

        def lose_failure_ack() -> None:
            nonlocal commit_calls
            commit_calls += 1
            real_commit()
            if commit_calls == 3:
                raise RuntimeError("synthetic failure commit acknowledgement lost")

        session.commit = lose_failure_ack  # type: ignore[method-assign]
        result = ImageJobService(
            session, store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=(
                ImageSourceSelection(product_id=asset.product_id, asset_id=asset.id),
            ),
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key="failure-commit-ack-loss",
        )

    assert result.job.status == "generation_failed"
    assert store.cleanup_attempts == 1
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, result.job.id).status == "generation_failed"  # type: ignore[union-attr]


def test_terminal_failure_commit_releases_store_proof_when_response_reload_raises(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    failing_store = FailSecondPublishStore(content_store)
    with api_session_factory() as session:
        service = ImageJobService(
            session, failing_store, DeterministicLocalImageGenerator()
        )
        real_reload = service._reload_attached_job

        def fail_terminal_response_reload(
            customer_id: UUID, job_id: UUID
        ) -> ImageOutputJob:
            reloaded = real_reload(customer_id, job_id)
            if reloaded.status == "generation_failed":
                raise RuntimeError("synthetic terminal response reload failed")
            return reloaded

        monkeypatch.setattr(
            service, "_reload_attached_job", fail_terminal_response_reload
        )
        with pytest.raises(
            RuntimeError, match="synthetic terminal response reload failed"
        ):
            service.create(
                CUSTOMER_A_ID,
                output_type="scene",
                sources=(
                    ImageSourceSelection(
                        product_id=asset.product_id, asset_id=asset.id
                    ),
                ),
                scene_style_id=scene.id,
                style_card_version_id=scene_version.id,
                output_ratio="1:1",
                idempotency_key="terminal-reload-registry-release",
            )

    with api_session_factory() as session:
        persisted = session.query(ImageOutputJob).one()
        assert persisted.status == "generation_failed"
        assert persisted.id not in image_jobs_module._LOCAL_JOB_STORES


def test_cleanup_failure_stays_pending_until_idempotent_replay_cleans_artifacts(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    store = FailPublishAndCleanupOnceStore(content_store)
    client.app.dependency_overrides[get_asset_content_store] = lambda: store
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    payload = _request([asset], scene, scene_version, idempotency_key="cleanup-retry")

    pending = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)

    assert pending.status_code == 201
    pending_body = pending.json()
    job_id = UUID(pending_body["id"])
    assert pending_body["status"] == "cleanup_pending"
    assert pending_body["failure_reason"] == (
        "synthetic_second_publish_failed;cleanup_pending:synthetic_cleanup_blocked"
    )
    assert pending_body["candidates"] == []
    assert len(content_store.list_derivatives(CUSTOMER_A_ID, job_id)) == 1
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 0
        job = session.get(ImageOutputJob, job_id)
        assert job is not None
        assert getattr(job, "artifact_store_identity", None) == (
            "local-synthetic-asset-store-v1"
        )
        assert getattr(job, "artifact_store_namespace", None)
        capability = getattr(job, "artifact_store_capability", None)
        assert isinstance(capability, str) and len(capability) == 64
        assert "artifact_store_capability" not in pending_body
        assert getattr(job, "expected_artifact_sequences", None) == [1, 2, 3]
        assert [event.event_type for event in session.query(AuditEvent).all()] == [
            "image_output.generation_requested",
            "image_output.cleanup_pending",
        ]

    recovered = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)

    assert recovered.status_code == 200
    assert recovered.json()["status"] == "generation_failed"
    assert recovered.json()["failure_reason"] == "synthetic_second_publish_failed"
    assert recovered.json()["candidates"] == []
    assert content_store.list_derivatives(CUSTOMER_A_ID, job_id) == ()
    with api_session_factory() as session:
        job = session.get(ImageOutputJob, job_id)
        assert job is not None and job.status == "generation_failed"
        assert [event.event_type for event in session.query(AuditEvent).all()] == [
            "image_output.generation_requested",
            "image_output.cleanup_pending",
            "image_output.cleanup_completed",
            "image_output.generation_failed",
        ]


def test_cleanup_outcome_commit_ack_loss_does_not_repeat_destructive_cleanup(
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    store = FailPublishAndCleanupOnceStore(content_store)
    source = (ImageSourceSelection(product_id=asset.product_id, asset_id=asset.id),)
    with api_session_factory() as session:
        pending = ImageJobService(
            session, store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=source,
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key="cleanup-commit-ack-loss",
        )
    assert pending.job.status == "cleanup_pending"

    with api_session_factory() as session:
        real_commit = session.commit
        commit_calls = 0

        def lose_cleanup_outcome_ack() -> None:
            nonlocal commit_calls
            commit_calls += 1
            real_commit()
            if commit_calls == 2:
                raise RuntimeError("synthetic cleanup commit acknowledgement lost")

        session.commit = lose_cleanup_outcome_ack  # type: ignore[method-assign]
        recovered = ImageJobService(
            session, store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=source,
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key="cleanup-commit-ack-loss",
        )

    assert recovered.job.status == "generation_failed"
    assert store.cleanup_attempts == 2
    with api_session_factory() as session:
        replay = ImageJobService(
            session, store, DeterministicLocalImageGenerator()
        ).create(
            CUSTOMER_A_ID,
            output_type="scene",
            sources=source,
            scene_style_id=scene.id,
            style_card_version_id=scene_version.id,
            output_ratio="1:1",
            idempotency_key="cleanup-commit-ack-loss",
        )
    assert replay.job.status == "generation_failed"
    assert store.cleanup_attempts == 2


class UnknownCleanupJobStore(FailSecondPublishStore):
    def cleanup_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[str, ...]:
        del customer_id, job_id
        return ()

    def cleanup_scope_state(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> str:
        del customer_id, job_id, expected_sequences, capability
        return "unknown"


class ForgedCleanStore(UnknownCleanupJobStore):
    """Copy public store labels but possess no store-issued cleanup authority."""

    def cleanup_scope_state(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> str:
        del customer_id, job_id, expected_sequences, capability
        return "cleaned"

    def verify_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> bool:
        del customer_id, job_id, expected_sequences, capability
        return False


class ExplicitlyAbandonedStore(FailPublishAndCleanupOnceStore):
    def __init__(self, delegate: LocalSyntheticAssetStore) -> None:
        super().__init__(delegate)
        self.explicitly_abandoned = False

    def cleanup_scope_state(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> str:
        if self.explicitly_abandoned:
            return "abandoned"
        return super().cleanup_scope_state(
            customer_id, job_id, expected_sequences, capability
        )


def test_verified_pre_cleanup_abandonment_becomes_terminal_without_cleanup_retry(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    content_store: LocalSyntheticAssetStore,
) -> None:
    store = ExplicitlyAbandonedStore(content_store)
    client.app.dependency_overrides[get_asset_content_store] = lambda: store
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    payload = _request([asset], scene, scene_version, idempotency_key="abandoned")
    pending = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)
    assert pending.json()["status"] == "cleanup_pending"
    attempts = store.cleanup_attempts
    store.explicitly_abandoned = True

    replay = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)

    assert replay.status_code == 200
    assert replay.json()["status"] == "cleanup_abandoned"
    assert store.cleanup_attempts == attempts


@pytest.mark.parametrize(
    "replacement_kind",
    ["different", "unknown", "unconfigured", "forged", "equivalent_proxy"],
)
def test_cleanup_replay_never_claims_success_for_wrong_or_unknown_store(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    failing_store = FailPublishAndCleanupOnceStore(content_store)
    client.app.dependency_overrides[get_asset_content_store] = lambda: failing_store
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    payload = _request(
        [asset], scene, scene_version, idempotency_key=f"scope-{replacement_kind}"
    )
    pending = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)
    job_id = UUID(pending.json()["id"])
    assert pending.json()["status"] == "cleanup_pending"

    other_store: LocalSyntheticAssetStore | None = None
    if replacement_kind == "different":
        other_store = LocalSyntheticAssetStore(tmp_path / "different-store")
        replacement: object = other_store
    elif replacement_kind == "unknown":
        replacement = UnknownCleanupJobStore(content_store)
    elif replacement_kind == "forged":
        replacement = ForgedCleanStore(content_store)
    elif replacement_kind == "equivalent_proxy":
        replacement = FailSecondPublishStore(content_store)
    else:
        replacement = UnconfiguredAssetContentStore()
    client.app.dependency_overrides[get_asset_content_store] = lambda: replacement
    try:
        replay = client.post(
            "/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload
        )
    finally:
        if other_store is not None:
            other_store.close()

    assert replay.status_code == 200
    assert replay.json()["status"] == "cleanup_pending"
    assert ";cleanup_pending:" in replay.json()["failure_reason"]
    assert len(content_store.list_derivatives(CUSTOMER_A_ID, job_id)) == 1
    with api_session_factory() as session:
        job = session.get(ImageOutputJob, job_id)
        assert job is not None and job.status == "cleanup_pending"
        assert session.query(ImageCandidate).count() == 0
        assert (
            session.query(AuditEvent)
            .order_by(AuditEvent.occurred_at)
            .all()[-1]
            .event_type
            == "image_output.cleanup_retry_failed"
        )

    client.app.dependency_overrides[get_asset_content_store] = lambda: failing_store
    recovered = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "generation_failed"
    assert content_store.list_derivatives(CUSTOMER_A_ID, job_id) == ()


def test_machine_check_replay_reverifies_all_persisted_artifacts(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    payload = _request(
        [asset], scene, scene_version, idempotency_key="missing-artifact"
    )
    created = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)
    job_id = UUID(created.json()["id"])
    assert content_store.cleanup_derivatives(CUSTOMER_A_ID, job_id) == ()

    replay = client.post("/v1/image-outputs/scene", headers=CUSTOMER_A, json=payload)
    status = client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_A)

    expected = {
        "error": {"code": "image_generation_outcome_uncertain", "retryable": True}
    }
    assert replay.status_code == 503
    assert replay.json() == expected
    assert status.status_code == 503
    assert status.json() == expected


def test_machine_check_get_rejects_proxy_with_copied_store_metadata(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    content_store: LocalSyntheticAssetStore,
) -> None:
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    created = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="fake-get-store"),
    )
    job_id = created.json()["id"]
    copied_proxy = FailSecondPublishStore(content_store)
    client.app.dependency_overrides[get_asset_content_store] = lambda: copied_proxy

    response = client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_A)

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "image_generation_outcome_uncertain", "retryable": True}
    }


class MetadataCopyStore(UnconfiguredAssetContentStore):
    def __init__(self, identity: str, namespace: str) -> None:
        super().__init__()
        self._identity = identity
        self._namespace = namespace

    @property
    def store_identity(self) -> str:
        return self._identity

    @property
    def store_namespace(self) -> str:
        return self._namespace

    def verify_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bool:
        del customer_id, job_id, sequence_number, expected_sha256
        return True


def test_machine_check_get_fails_closed_after_original_store_is_garbage_collected(
    client: TestClient,
    tmp_path: Path,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
) -> None:
    original = LocalSyntheticAssetStore(tmp_path / "restart-store")
    reference = weakref.ref(original)
    original_holder = {"store": original}
    client.app.dependency_overrides[get_asset_content_store] = lambda: original_holder[
        "store"
    ]
    _, scene, scene_version = style_factory()
    asset = asset_factory()
    created = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="restart-get"),
    )
    job_id = created.json()["id"]
    copied = MetadataCopyStore(original.store_identity, original.store_namespace)
    client.app.dependency_overrides[get_asset_content_store] = lambda: copied
    original.close()
    original_holder.clear()
    del original
    gc.collect()
    assert reference() is None

    response = client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_A)

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "image_generation_outcome_uncertain", "retryable": True}
    }


class MutableIdentityGenerator(DeterministicLocalImageGenerator):
    """Change provider identity during generation to test the pinned snapshot."""

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        candidates = super().generate(request)
        self.adapter_version = "mutated-provider-v9"
        self.generation_mode = "mutated_mode"
        return candidates


def test_adapter_identity_change_during_generation_fails_with_fixed_audit_identity(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> None:
    mutable = DeterministicLocalImageGenerator()
    baseline = DeterministicLocalImageGenerator()

    def mutate_during_generation(
        request: GenerationRequest,
    ) -> list[GeneratedCandidate]:
        candidates = baseline.generate(request)
        mutable.adapter_version = "mutated-provider-v9"
        mutable.generation_mode = "mutated_mode"
        return candidates

    mutable.generate = mutate_during_generation  # type: ignore[method-assign]
    client.app.dependency_overrides[get_image_generator] = lambda: mutable
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="mutable-adapter"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "generation_failed"
    assert body["failure_reason"] == "image_generator_identity_changed"
    assert body["adapter_version"] == "deterministic-local-image-v1"
    assert body["generation_mode"] == "synthetic_test_only"
    assert body["candidates"] == []
    assert content_store.list_derivatives(CUSTOMER_A_ID, UUID(body["id"])) == ()
    with api_session_factory() as session:
        job = session.query(ImageOutputJob).one()
        assert job.adapter_version == "deterministic-local-image-v1"
        assert session.query(ImageCandidate).count() == 0
        for event in session.query(AuditEvent).all():
            assert event.payload["adapter_version"] == "deterministic-local-image-v1"
            assert event.payload["generation_mode"] == "synthetic_test_only"


class UnsupportedIdentityGenerator(DeterministicLocalImageGenerator):
    adapter_version = "not-approved-v1"


def test_non_deterministic_adapter_identity_is_rejected_before_any_mutation(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    client.app.dependency_overrides[get_image_generator] = lambda: (
        UnsupportedIdentityGenerator()
    )
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="bad-adapter"),
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "unsupported_image_generator", "retryable": False}
    }
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0
        assert session.query(AuditEvent).count() == 0


class PublishableGenerator(DeterministicLocalImageGenerator):
    publishable = True


def test_publishable_generator_is_rejected_before_any_mutation(
    client: TestClient,
    style_factory: Callable[..., tuple[StyleCardVersion, SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    client.app.dependency_overrides[get_image_generator] = lambda: (
        PublishableGenerator()
    )
    _, scene, scene_version = style_factory()
    asset = asset_factory()

    response = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_request([asset], scene, scene_version, idempotency_key="publishable"),
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "unsupported_image_generator", "retryable": False}
    }
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_one_generic_status_route_has_discriminated_openapi_contract() -> None:
    app = create_app()
    application_routes = [
        nested_route
        for included in app.routes
        for nested_route in getattr(
            getattr(included, "original_router", None), "routes", ()
        )
    ]
    matching_routes = [
        route
        for route in application_routes
        if getattr(route, "path", None) == "/v1/image-outputs/{job_id}"
        and "GET" in (getattr(route, "methods", None) or set())
    ]

    assert len(matching_routes) == 1
    openapi = app.openapi()
    response_schema = openapi["paths"]["/v1/image-outputs/{job_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    if "$ref" in response_schema:
        response_schema = openapi["components"]["schemas"][
            response_schema["$ref"].rsplit("/", maxsplit=1)[-1]
        ]
    assert response_schema["discriminator"]["propertyName"] == "output_type"
    assert sorted(response_schema["discriminator"]["mapping"]) == [
        "multi_product",
        "product_style",
        "real_photo",
        "scene",
        "wear",
    ]
    assert len(response_schema["oneOf"]) == 2
