"""Real-photo normalization contracts using synthetic image fixtures only."""

import gc
import json
import os
import weakref
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

import opc_style.domain.image_normalizer as image_normalizer_module
import opc_style.services.real_photos as real_photos_module
from opc_style.api.dependencies import get_session
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_normalizer import (
    ImageNormalizationError,
    ImageNormalizer,
    NormalizationProfile,
)
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
)
from opc_style.services.real_photos import (
    STORE_IDENTITY_FILENAME,
    AssetContentStoreError,
    LocalSyntheticAssetStore,
    RealPhotoOutputService,
)

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _artifact_entries(root: Path) -> list[Path]:
    return [entry for entry in root.iterdir() if entry.name != STORE_IDENTITY_FILENAME]


def _jpeg(path: Path, size: tuple[int, int] = (1600, 800)) -> None:
    Image.new("RGB", size, (18, 96, 144)).save(
        path,
        format="JPEG",
        quality=95,
        exif=Image.Exif(),
        comment=b"synthetic metadata to strip",
    )


def _oriented_jpeg(path: Path) -> None:
    image = Image.new("RGB", (120, 80), (32, 64, 128))
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "synthetic author to strip"
    image.save(path, format="JPEG", quality=95, exif=exif)


@pytest.fixture
def profile() -> NormalizationProfile:
    return NormalizationProfile.catalog_v1(max_width=50, max_height=50)


def test_normalizer_applies_exif_orientation_and_bounded_aspect_resize(
    tmp_path: Path, profile: NormalizationProfile
) -> None:
    source = tmp_path / "oriented.jpg"
    _oriented_jpeg(source)

    result = ImageNormalizer().normalize(source, profile)

    assert result.width == 33
    assert result.height == 50
    assert result.metadata["source_dimensions"] == [120, 80]
    assert result.metadata["oriented_dimensions"] == [80, 120]
    assert result.metadata["exif_orientation"] == 6
    assert result.metadata["orientation_applied"] is True
    with Image.open(BytesIO(result.content)) as normalized:
        assert normalized.size == (33, 50)
        assert normalized.mode == "RGB"
        assert normalized.getexif().get(274) is None
        assert normalized.getexif().get(315) is None


def test_normalizer_returns_payload_without_publishing_any_path(
    tmp_path: Path, profile: NormalizationProfile
) -> None:
    source = tmp_path / "oriented.jpg"
    _oriented_jpeg(source)
    original_bytes = source.read_bytes()

    result = ImageNormalizer().normalize(source, profile)

    assert result.content_sha256 == sha256(result.content).hexdigest()
    assert result.width == 33
    assert result.height == 50
    assert source.read_bytes() == original_bytes
    assert sorted(path.name for path in tmp_path.iterdir()) == ["oriented.jpg"]
    with Image.open(BytesIO(result.content)) as normalized:
        assert normalized.size == (33, 50)
        assert normalized.format == "JPEG"


def test_normalizer_converts_png_to_srgb_jpeg_and_strips_metadata(
    tmp_path: Path, profile: NormalizationProfile
) -> None:
    source = tmp_path / "transparent.png"
    Image.new("RGBA", (40, 20), (200, 30, 20, 128)).save(
        source, format="PNG", pnginfo=None
    )

    result = ImageNormalizer().normalize(source, profile)

    assert result.width == 40
    assert result.height == 20
    assert result.mime_type == "image/jpeg"
    assert result.metadata["source_format"] == "PNG"
    assert result.metadata["source_mode"] == "RGBA"
    assert result.metadata["color_space"] == "sRGB"
    assert result.metadata["jpeg_quality"] == 92
    with Image.open(BytesIO(result.content)) as normalized:
        assert normalized.format == "JPEG"
        assert normalized.mode == "RGB"
        assert "exif" not in normalized.info
        assert "comment" not in normalized.info


def test_normalizer_never_upscales_and_is_byte_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "small.jpg"
    _jpeg(source, (20, 10))
    profile = NormalizationProfile.catalog_v1(max_width=100, max_height=100)

    first_result = ImageNormalizer().normalize(source, profile)
    second_result = ImageNormalizer().normalize(source, profile)

    assert (first_result.width, first_result.height) == (20, 10)
    assert first_result.content_sha256 == second_result.content_sha256
    assert first_result.content == second_result.content
    assert first_result.metadata == second_result.metadata


def test_normalizer_rejects_invalid_exif_orientation_without_artifact(
    tmp_path: Path, profile: NormalizationProfile
) -> None:
    source = tmp_path / "invalid-orientation.jpg"
    image = Image.new("RGB", (40, 20), (10, 20, 30))
    exif = Image.Exif()
    exif[274] = 9
    image.save(source, format="JPEG", exif=exif)

    with pytest.raises(ImageNormalizationError, match="invalid_exif_orientation"):
        ImageNormalizer().normalize(source, profile)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "invalid-orientation.jpg"
    ]


def test_normalizer_hash_failure_leaves_only_immutable_input(
    tmp_path: Path,
    profile: NormalizationProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "original.jpg"
    _jpeg(source, (40, 20))
    original_bytes = source.read_bytes()

    def fail_hash(content: bytes) -> str:
        del content
        raise RuntimeError("synthetic hash failure")

    monkeypatch.setattr(image_normalizer_module, "_digest_payload", fail_hash)

    with pytest.raises(RuntimeError, match="synthetic hash failure"):
        ImageNormalizer().normalize(source, profile)

    assert source.read_bytes() == original_bytes
    assert sorted(path.name for path in tmp_path.iterdir()) == ["original.jpg"]


@pytest.mark.parametrize("kind", ["unsupported", "corrupt"])
def test_normalizer_fails_closed_without_an_artifact(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "input.bin"
    if kind == "unsupported":
        Image.new("RGB", (20, 20), (1, 2, 3)).save(source, format="GIF")
        expected = "unsupported_image_format"
    else:
        source.write_bytes(b"not an image")
        expected = "corrupt_image_input"

    with pytest.raises(ImageNormalizationError, match=expected):
        ImageNormalizer().normalize(
            source,
            NormalizationProfile.catalog_v1(max_width=50, max_height=50),
        )

    assert sorted(path.name for path in tmp_path.iterdir()) == ["input.bin"]


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'real-photo-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def content_store(
    tmp_path: Path,
) -> Generator[LocalSyntheticAssetStore, None, None]:
    with LocalSyntheticAssetStore(tmp_path / "synthetic-assets") as store:
        yield store


@pytest.fixture
def client(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> Generator[TestClient, None, None]:
    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_asset_content_store] = lambda: content_store
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def asset_factory(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    tmp_path: Path,
) -> Callable[..., tuple[ProductAsset, Path]]:
    def create_asset(
        *,
        customer_id: UUID = CUSTOMER_A_ID,
        product_id: str = "product-001",
        permitted: bool = True,
        source_kind: str = "jpeg",
    ) -> tuple[ProductAsset, Path]:
        source = tmp_path / f"{uuid4()}.{source_kind}"
        if source_kind == "jpeg":
            _jpeg(source)
            mime_type = "image/jpeg"
            width, height = (1600, 800)
        elif source_kind == "gif":
            Image.new("RGB", (40, 20), (5, 10, 15)).save(source, format="GIF")
            mime_type = "image/jpeg"
            width, height = (40, 20)
        else:
            source.write_bytes(b"corrupt synthetic image")
            mime_type = "image/jpeg"
            width, height = (40, 20)
        with api_session_factory() as session:
            customer = session.get(Customer, customer_id)
            if customer is None:
                session.add(Customer(id=customer_id, display_name="Synthetic customer"))
                session.flush()
            asset = ProductAsset(
                customer_id=customer_id,
                product_id=product_id,
                photo_id=f"photo-{uuid4()}",
                identity="original_real_photo",
                confirmed=True,
                source_type="synthetic_metadata",
                quality_flags=["synthetic_fixture"],
                occurred_at=datetime(2026, 8, 30, tzinfo=UTC),
                content_sha256=_digest(source),
                mime_type=mime_type,
                width=width,
                height=height,
                storage_path=None,
            )
            session.add(asset)
            session.flush()
            session.add(
                ProductAssetPermissionEvent(
                    customer_id=customer_id,
                    product_asset_id=asset.id,
                    transform_permission=permitted,
                    occurred_at=datetime(2026, 8, 30, tzinfo=UTC),
                    provenance="synthetic_test",
                )
            )
            session.commit()
            session.refresh(asset)
            session.expunge(asset)
        content_store.register_original(customer_id, asset.id, source)
        return asset, source

    return create_asset


def _request(asset: ProductAsset, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product_id": asset.product_id,
        "asset_id": str(asset.id),
        "profile": "catalog-v1",
        "idempotency_key": "normalize-request-001",
    }
    payload.update(overrides)
    return payload


def test_real_photo_job_creates_three_normalized_fact_candidates(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
) -> None:
    asset, source = asset_factory()
    original_bytes = source.read_bytes()

    response = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["output_type"] == "real_photo"
    assert body["profile"] == "catalog-v1"
    assert body["adapter_version"] == "pillow-catalog-v1"
    assert body["status"] == "machine_check"
    assert body["failure_reason"] is None
    assert body["publishable"] is False
    assert len(body["candidates"]) == 3
    assert [
        (candidate["width"], candidate["height"]) for candidate in body["candidates"]
    ] == [(512, 256), (1024, 512), (1600, 800)]
    assert [candidate["sequence_number"] for candidate in body["candidates"]] == [
        1,
        2,
        3,
    ]
    for candidate in body["candidates"]:
        assert candidate["identity"] == "normalized_real_photo_derivative"
        assert candidate["candidate_state"] == "pending_authenticity_check"
        assert candidate["metadata"]["generation_mode"] == "real_photo_normalization"
        assert candidate["metadata"]["is_ai_generated"] is False
        assert candidate["metadata"]["publishable"] is False
        assert "artifact_path" not in candidate

    assert source.read_bytes() == original_bytes
    assert _digest(source) == asset.content_sha256
    with api_session_factory() as session:
        stored_asset = session.get(ProductAsset, asset.id)
        candidates = (
            session.query(ImageCandidate).order_by(ImageCandidate.sequence_number).all()
        )
        assert stored_asset is not None
        assert stored_asset.content_sha256 == asset.content_sha256
        assert stored_asset.storage_path is None
        assert session.query(ImageOutputJob).count() == 1
        assert session.query(ProductAssetLink).count() == 1
        assert len(candidates) == 3
        for candidate in candidates:
            path = Path(candidate.artifact_path)
            assert path.is_file()
            assert _digest(path) == candidate.content_sha256


def test_real_photo_job_is_customer_scoped_and_idempotent(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
) -> None:
    asset, _ = asset_factory()
    created = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )
    replay = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == created.json()
    job_id = created.json()["id"]
    assert (
        client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_A).status_code == 200
    )
    assert (
        client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_B).status_code == 404
    )
    foreign_create = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_B, json=_request(asset)
    )
    assert foreign_create.status_code == 404
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 1
        assert session.query(ImageCandidate).count() == 3


def test_idempotency_key_cannot_be_reused_for_different_facts(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
) -> None:
    first, _ = asset_factory(product_id="product-001")
    second, _ = asset_factory(product_id="product-002")
    assert (
        client.post(
            "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(first)
        ).status_code
        == 201
    )

    conflict = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(second)
    )

    assert conflict.status_code == 409
    assert conflict.json() == {
        "error": {"code": "image_output_idempotency_conflict", "retryable": False}
    }


def test_concurrent_identical_real_photo_requests_replay_without_leaking_jobs(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
) -> None:
    asset, _ = asset_factory()
    barrier = Barrier(2)

    def submit() -> tuple[int, dict[str, Any]]:
        barrier.wait()
        response = client.post(
            "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))

    assert sorted(status for status, _ in results) == [200, 201]
    assert results[0][1] == results[1][1]
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 1
        assert session.query(ImageCandidate).count() == 3


def test_concurrent_conflicting_real_photo_requests_return_201_and_409(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
) -> None:
    first, _ = asset_factory(product_id="concurrent-first")
    second, _ = asset_factory(product_id="concurrent-second")
    barrier = Barrier(2)

    def submit(asset: ProductAsset) -> int:
        barrier.wait()
        return client.post(
            "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(submit, [first, second]))

    assert sorted(statuses) == [201, 409]
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 1
        assert session.query(ImageCandidate).count() == 3


def test_real_photo_commit_ack_loss_recovers_only_after_durable_artifact_proof(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, _ = asset_factory()
    original_commit = Session.commit
    lost = False

    def commit_then_lose_ack(session: Session) -> None:
        nonlocal lost
        is_completion = any(
            isinstance(item, AuditEvent)
            and item.event_type == "image_output.real_photo_completed"
            for item in session.new
        )
        original_commit(session)
        if is_completion and not lost:
            lost = True
            raise RuntimeError("synthetic completion acknowledgement loss")

    monkeypatch.setattr(Session, "commit", commit_then_lose_ack)
    response = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "machine_check"
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 3
        assert session.query(ImageOutputJob).one().status == "machine_check"


def test_real_photo_commit_ack_loss_with_missing_artifact_is_uncertain_not_failed(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, _ = asset_factory()
    original_commit = Session.commit
    injected = False

    def commit_delete_artifact_then_lose_ack(session: Session) -> None:
        nonlocal injected
        is_completion = any(
            isinstance(item, AuditEvent)
            and item.event_type == "image_output.real_photo_completed"
            for item in session.new
        )
        original_commit(session)
        if is_completion and not injected:
            injected = True
            with api_session_factory() as verification:
                candidate = (
                    verification.query(ImageCandidate)
                    .order_by(ImageCandidate.sequence_number)
                    .first()
                )
                assert candidate is not None
                Path(candidate.artifact_path).unlink()
            raise RuntimeError("synthetic completion acknowledgement loss")

    monkeypatch.setattr(Session, "commit", commit_delete_artifact_then_lose_ack)
    response = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "image_generation_outcome_uncertain"
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).one().status == "machine_check"
        assert session.query(ImageCandidate).count() == 3


def test_real_photo_candidate_commit_before_persist_cleans_and_records_failure(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, _ = asset_factory()
    original_commit = Session.commit

    def fail_before_completion_commit(session: Session) -> None:
        if any(
            isinstance(item, AuditEvent)
            and item.event_type == "image_output.real_photo_completed"
            for item in session.new
        ):
            raise RuntimeError("synthetic commit failed before persistence")
        original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_before_completion_commit)
    response = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "generation_failed"
    job_id = UUID(response.json()["id"])
    assert content_store.list_derivatives(CUSTOMER_A_ID, job_id) == ()
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).one().status == "generation_failed"
        assert session.query(ImageCandidate).count() == 0


def test_real_photo_unpersisted_commit_cleanup_failure_retries_on_replay(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, _ = asset_factory()
    original_commit = Session.commit
    original_cleanup = content_store.cleanup_scoped_derivatives
    cleanup_attempts = 0

    def fail_before_completion_commit(session: Session) -> None:
        if any(
            isinstance(item, AuditEvent)
            and item.event_type == "image_output.real_photo_completed"
            for item in session.new
        ):
            raise RuntimeError("synthetic commit failed before persistence")
        original_commit(session)

    def fail_cleanup_once(
        customer_id: UUID, job_id: UUID, capability: str
    ) -> tuple[str, ...]:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            return ("asset_store_cleanup_failed",)
        return original_cleanup(customer_id, job_id, capability)

    monkeypatch.setattr(Session, "commit", fail_before_completion_commit)
    monkeypatch.setattr(
        content_store, "cleanup_scoped_derivatives", fail_cleanup_once
    )
    created = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "cleanup_pending"
    job_id = UUID(created.json()["id"])
    assert len(content_store.list_derivatives(CUSTOMER_A_ID, job_id)) == 3

    replay = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "generation_failed"
    assert content_store.list_derivatives(CUSTOMER_A_ID, job_id) == ()
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 0


def test_real_photo_machine_check_fails_closed_when_artifact_is_missing(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
) -> None:
    asset, _ = asset_factory()
    created = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )
    assert created.status_code == 201
    with api_session_factory() as session:
        candidate = session.query(ImageCandidate).order_by(ImageCandidate.sequence_number).first()
        assert candidate is not None
        Path(candidate.artifact_path).unlink()

    replay = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )
    fetched = client.get(f"/v1/image-outputs/{created.json()['id']}", headers=CUSTOMER_A)

    for response in (replay, fetched):
        assert response.status_code == 503
        assert response.json()["error"] == {
            "code": "image_generation_outcome_uncertain",
            "retryable": True,
        }


def test_permission_denial_blocks_job_before_any_output_mutation(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
) -> None:
    asset, _ = asset_factory(permitted=False)

    response = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "transform_permission_denied"
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0
        assert session.query(ImageCandidate).count() == 0
        assert session.query(ProductAssetLink).count() == 0


@pytest.mark.parametrize(
    ("source_kind", "failure_reason"),
    [("gif", "unsupported_image_format"), ("corrupt", "corrupt_image_input")],
)
def test_bad_input_records_failed_job_without_partial_candidates(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
    source_kind: str,
    failure_reason: str,
) -> None:
    asset, source = asset_factory(source_kind=source_kind)
    original_hash = _digest(source)

    response = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "generation_failed"
    assert response.json()["failure_reason"] == failure_reason
    assert response.json()["candidates"] == []
    assert _digest(source) == original_hash
    with api_session_factory() as session:
        assert session.query(ImageOutputJob).one().status == "generation_failed"
        assert session.query(ImageCandidate).count() == 0
        event_types = [event.event_type for event in session.query(AuditEvent).all()]
        assert "image_output.real_photo_failed" in event_types


def test_hash_mismatch_fails_closed_without_reading_as_trusted_original(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
) -> None:
    asset, source = asset_factory()
    source.write_bytes(source.read_bytes() + b"tampered")

    response = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )

    assert response.status_code == 201
    assert response.json()["failure_reason"] == "source_content_hash_mismatch"
    assert response.json()["candidates"] == []
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 0


@pytest.mark.parametrize(
    "path_field", ["input_path", "output_path", "storage_path", "file_path"]
)
def test_http_contract_rejects_all_filesystem_paths(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    path_field: str,
) -> None:
    asset, _ = asset_factory()

    response = client.post(
        "/v1/image-outputs/real-photo",
        headers=CUSTOMER_A,
        json=_request(asset, **{path_field: "/tmp/customer-photo.jpg"}),
    )

    assert response.status_code == 422


def test_candidate_facts_are_immutable_and_state_is_not_persisted(
    client: TestClient,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    api_session_factory: sessionmaker[Session],
) -> None:
    asset, _ = asset_factory()
    response = client.post(
        "/v1/image-outputs/real-photo", headers=CUSTOMER_A, json=_request(asset)
    )
    assert response.status_code == 201

    with api_session_factory() as session:
        candidate = session.query(ImageCandidate).first()
        assert candidate is not None
        assert not hasattr(candidate, "status")
        candidate.width = 1
        with pytest.raises(DomainRuleError, match="image_candidate_immutable"):
            session.flush()


def test_service_cleans_staged_artifacts_when_later_candidate_fails(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
) -> None:
    asset, _ = asset_factory()

    class FailOnSecondNormalizer:
        def __init__(self) -> None:
            self.calls = 0
            self.delegate = ImageNormalizer()

        def normalize(
            self,
            input_path: Path,
            profile: NormalizationProfile,
        ) -> Any:
            self.calls += 1
            if self.calls == 2:
                raise ImageNormalizationError("synthetic_second_candidate_failure")
            return self.delegate.normalize(input_path, profile)

    with api_session_factory() as session:
        result = RealPhotoOutputService(
            session, content_store, normalizer=FailOnSecondNormalizer()
        ).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v1",
            idempotency_key="partial-failure-test",
        )

    assert result.job.status == "generation_failed"
    assert result.candidates == ()
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 0
    assert content_store.list_derivatives(CUSTOMER_A_ID, result.job.id) == ()


def test_unexpected_candidate_error_becomes_sanitized_idempotent_failed_job(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
) -> None:
    asset, _ = asset_factory()

    class RuntimeFailureOnSecondCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def normalize(
            self,
            input_path: Path,
            profile: NormalizationProfile,
        ) -> Any:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("/private/customer/secret.jpg must not escape")
            return ImageNormalizer().normalize(input_path, profile)

    normalizer = RuntimeFailureOnSecondCandidate()
    with api_session_factory() as session:
        created = RealPhotoOutputService(
            session, content_store, normalizer=normalizer
        ).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v1",
            idempotency_key="unexpected-runtime-failure",
        )
    with api_session_factory() as session:
        replay = RealPhotoOutputService(
            session, content_store, normalizer=normalizer
        ).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v1",
            idempotency_key="unexpected-runtime-failure",
        )

    assert created.job.status == "generation_failed"
    assert created.job.failure_reason == "normalization_unexpected_failure"
    assert created.candidates == ()
    assert replay.job.id == created.job.id
    assert replay.created is False
    assert normalizer.calls == 2
    assert content_store.list_derivatives(CUSTOMER_A_ID, created.job.id) == ()
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 0
        failed_events = (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "image_output.real_photo_failed")
            .all()
        )
        assert len(failed_events) == 1
        assert "/private" not in str(failed_events[0].payload)


def test_cleanup_exception_does_not_block_sanitized_failed_state(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
) -> None:
    asset, _ = asset_factory()

    class CleanupFailureStore:
        @property
        def store_identity(self) -> str:
            return content_store.store_identity

        @property
        def store_namespace(self) -> str:
            return content_store.store_namespace

        def issue_cleanup_capability(
            self,
            customer_id: UUID,
            job_id: UUID,
            expected_sequences: tuple[int, ...],
        ) -> str:
            return content_store.issue_cleanup_capability(
                customer_id, job_id, expected_sequences
            )

        def resolve_original(self, customer_id: UUID, asset_id: UUID) -> Path:
            return content_store.resolve_original(customer_id, asset_id)

        def reserve_derivative(
            self, customer_id: UUID, job_id: UUID, sequence_number: int
        ) -> Any:
            return content_store.reserve_derivative(
                customer_id, job_id, sequence_number
            )

        def create_derivative(self, target: Any, payload: Any) -> Any:
            return content_store.create_derivative(target, payload)

        def cleanup_derivatives(
            self, customer_id: UUID, job_id: UUID
        ) -> tuple[str, ...]:
            raise RuntimeError("/private/cleanup/path")

        def verify_cleanup_capability(
            self,
            customer_id: UUID,
            job_id: UUID,
            expected_sequences: tuple[int, ...],
            capability: str,
        ) -> bool:
            return content_store.verify_cleanup_capability(
                customer_id, job_id, expected_sequences, capability
            )

        def cleanup_scope_state(
            self,
            customer_id: UUID,
            job_id: UUID,
            expected_sequences: tuple[int, ...],
            capability: str,
        ) -> str:
            return content_store.cleanup_scope_state(
                customer_id, job_id, expected_sequences, capability
            )

        def cleanup_scoped_derivatives(
            self, customer_id: UUID, job_id: UUID, capability: str
        ) -> tuple[str, ...]:
            raise RuntimeError("/private/cleanup/path")

    class AlwaysFails:
        def normalize(
            self,
            input_path: Path,
            profile: NormalizationProfile,
        ) -> Any:
            del input_path, profile
            raise RuntimeError("unexpected")

    with api_session_factory() as session:
        result = RealPhotoOutputService(
            session, CleanupFailureStore(), normalizer=AlwaysFails()
        ).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v1",
            idempotency_key="cleanup-failure",
        )

    assert result.job.status == "cleanup_pending"
    assert result.job.failure_reason is not None
    assert result.job.failure_reason.startswith("normalization_unexpected_failure")
    with api_session_factory() as session:
        failed = (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "image_output.cleanup_retry_failed")
            .one()
        )
        assert failed.payload["cleanup_errors"] == ["asset_store_cleanup_failed"]
        assert "/private" not in str(failed.payload)


def test_real_photo_cleanup_pending_retries_to_verified_generation_failed(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset, _ = asset_factory()
    original_cleanup = content_store.cleanup_scoped_derivatives
    attempts = 0

    def fail_once(customer_id: UUID, job_id: UUID, capability: str) -> tuple[str, ...]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ("asset_store_cleanup_failed",)
        return original_cleanup(customer_id, job_id, capability)

    monkeypatch.setattr(content_store, "cleanup_scoped_derivatives", fail_once)

    class FailSecond:
        calls = 0

        def normalize(self, input_path: Path, profile: NormalizationProfile) -> Any:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("synthetic partial publish failure")
            return ImageNormalizer().normalize(input_path, profile)

    with api_session_factory() as session:
        created = RealPhotoOutputService(
            session, content_store, normalizer=FailSecond()
        ).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v1",
            idempotency_key="cleanup-pending-retry",
        )
    assert created.job.status == "cleanup_pending"
    assert content_store.list_derivatives(CUSTOMER_A_ID, created.job.id)

    with api_session_factory() as session:
        replay = RealPhotoOutputService(session, content_store).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v1",
            idempotency_key="cleanup-pending-retry",
        )

    assert replay.created is False
    assert replay.job.status == "generation_failed"
    assert replay.candidates == ()
    assert content_store.list_derivatives(CUSTOMER_A_ID, created.job.id) == ()


def test_service_rejects_unsupported_profile_before_any_write(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
) -> None:
    asset, _ = asset_factory()

    with (
        api_session_factory() as session,
        pytest.raises(DomainRuleError, match="unsupported_normalization_profile"),
    ):
        RealPhotoOutputService(session, content_store).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v2",
            idempotency_key="unsupported-profile",
        )

    with api_session_factory() as session:
        assert session.query(ImageOutputJob).count() == 0
        assert session.query(ImageCandidate).count() == 0
        assert session.query(ProductAssetLink).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_synthetic_store_cleanup_preserves_unrecorded_files(tmp_path: Path) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    artifact = store.create_derivative(
        target, _normalized_payload(b"owned staging")
    ).output_path
    unrelated = artifact.parent / "external.keep"
    unrelated.write_bytes(b"not owned by the store call")

    cleanup_errors = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert cleanup_errors == ("asset_store_cleanup_unowned_entries",)
    assert artifact.is_file()
    assert unrelated.read_bytes() == b"not owned by the store call"


def test_synthetic_store_rolls_back_error_between_publish_and_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    payload = _normalized_payload(b"published candidate")

    def fail_before_registration(path: Path, identity: tuple[int, int]) -> None:
        del path, identity
        raise RuntimeError("synthetic publish-registration failure")

    monkeypatch.setattr(
        real_photos_module,
        "_after_candidate_publish_before_registration",
        fail_before_registration,
        raising=False,
    )

    with pytest.raises(
        AssetContentStoreError, match="asset_derivative_publish_failed"
    ):
        store.create_derivative(target, payload)

    assert store.list_derivatives(CUSTOMER_A_ID, job_id) == ()
    assert not target.artifact_path.exists()
    assert _artifact_entries(tmp_path / "store") == []


def test_synthetic_store_rejects_silent_replacement_before_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    displaced = tmp_path / "displaced-published-candidate"

    def replace_before_registration(path: Path, identity: tuple[int, int]) -> None:
        del identity
        path.rename(displaced)
        path.write_bytes(b"external replacement")

    monkeypatch.setattr(
        real_photos_module,
        "_after_candidate_publish_before_registration",
        replace_before_registration,
    )

    with pytest.raises(
        AssetContentStoreError, match="asset_derivative_publish_identity_mismatch"
    ):
        store.create_derivative(target, _normalized_payload(b"owned candidate"))

    assert target.artifact_path.read_bytes() == b"external replacement"
    assert displaced.read_bytes() == b"owned candidate"
    assert store.list_derivatives(CUSTOMER_A_ID, job_id) == ()


def test_synthetic_store_rolls_back_without_swallowing_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)

    def interrupt_after_publish(path: Path, identity: tuple[int, int]) -> None:
        del path, identity
        raise KeyboardInterrupt

    monkeypatch.setattr(
        real_photos_module,
        "_after_candidate_publish_before_registration",
        interrupt_after_publish,
    )

    with pytest.raises(KeyboardInterrupt):
        store.create_derivative(target, _normalized_payload(b"candidate"))

    assert _artifact_entries(tmp_path / "store") == []


def test_synthetic_store_direct_publish_has_only_the_final_file(
    tmp_path: Path,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)

    result = store.create_derivative(
        target, _normalized_payload(b"direct final candidate")
    )

    assert result.output_path == target.artifact_path
    assert target.artifact_path.read_bytes() == b"direct final candidate"
    assert [path.name for path in target.artifact_path.parent.iterdir()] == [
        "candidate-1.jpg"
    ]


def test_synthetic_store_context_manager_closes_explicitly_and_idempotently(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.jpg"
    _jpeg(original)
    store = LocalSyntheticAssetStore(tmp_path / "store")
    asset_id = uuid4()
    store.register_original(CUSTOMER_A_ID, asset_id, original)
    target = store.reserve_derivative(CUSTOMER_A_ID, uuid4(), 1)
    root_descriptor = store._root_fd

    with store as entered:
        assert entered is store
        os.fstat(root_descriptor)

    with pytest.raises(OSError):
        os.fstat(root_descriptor)
    store.close()
    store.close()
    closed_operations = (
        lambda: store.register_original(CUSTOMER_A_ID, asset_id, original),
        lambda: store.resolve_original(CUSTOMER_A_ID, asset_id),
        lambda: store.reserve_derivative(CUSTOMER_A_ID, uuid4(), 1),
        lambda: store.create_derivative(
            target, _normalized_payload(b"closed store")
        ),
        lambda: store.cleanup_derivatives(CUSTOMER_A_ID, target.job_id),
        lambda: store.verify_derivative(
            CUSTOMER_A_ID, target.job_id, 1, sha256(b"closed store").hexdigest()
        ),
    )
    for operation in closed_operations:
        with pytest.raises(AssetContentStoreError, match="asset_content_store_closed"):
            operation()
    with pytest.raises(AssetContentStoreError, match="asset_content_store_closed"):
        store.__enter__()


def test_synthetic_store_gc_fallback_releases_unclosed_root_descriptor(
    tmp_path: Path,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    root_descriptor = store._root_fd
    store_reference = weakref.ref(store)

    del store
    gc.collect()

    assert store_reference() is None
    with pytest.raises(OSError):
        os.fstat(root_descriptor)


def test_synthetic_store_constructor_exception_closes_open_root_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open_directory = real_photos_module._open_directory
    opened_descriptors: list[int] = []

    def track_open_directory(path: Path) -> int:
        descriptor = real_open_directory(path)
        opened_descriptors.append(descriptor)
        return descriptor

    def interrupt_identity_check(descriptor: int) -> tuple[int, int]:
        del descriptor
        raise SystemExit(73)

    monkeypatch.setattr(real_photos_module, "_open_directory", track_open_directory)
    monkeypatch.setattr(
        real_photos_module, "_identity_from_fd", interrupt_identity_check
    )

    with pytest.raises(SystemExit, match="73"):
        LocalSyntheticAssetStore(tmp_path / "store")

    assert len(opened_descriptors) == 1
    gc.collect()
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])


def test_synthetic_store_lock_constructor_base_exception_closes_root_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open_directory = real_photos_module._open_directory
    opened_descriptors: list[int] = []

    def track_open_directory(path: Path) -> int:
        descriptor = real_open_directory(path)
        opened_descriptors.append(descriptor)
        return descriptor

    def interrupt_lock_construction() -> None:
        raise SystemExit(74)

    monkeypatch.setattr(real_photos_module, "_open_directory", track_open_directory)
    monkeypatch.setattr(
        real_photos_module,
        "threading",
        SimpleNamespace(RLock=interrupt_lock_construction),
    )

    with pytest.raises(SystemExit, match="74") as retained_exception:
        LocalSyntheticAssetStore(tmp_path / "store")

    assert retained_exception.value.code == 74
    assert len(opened_descriptors) == 1
    gc.collect()
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])


def test_synthetic_store_post_return_tampering_invalidates_path_attestation(
    tmp_path: Path,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    result = store.create_derivative(
        target, _normalized_payload(b"owned candidate")
    )
    assert store.verify_derivative(
        CUSTOMER_A_ID, job_id, 1, result.content_sha256
    )

    result.output_path.unlink()
    result.output_path.write_bytes(b"same-uid post-return replacement")

    assert not store.verify_derivative(
        CUSTOMER_A_ID, job_id, 1, result.content_sha256
    )


def test_synthetic_store_rejects_cross_customer_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    foreign_job = uuid4()
    foreign_target = store.reserve_derivative(CUSTOMER_B_ID, foreign_job, 1)
    foreign_artifact = store.create_derivative(
        foreign_target, _normalized_payload(b"customer-b-owned")
    ).output_path

    escaped_job = uuid4()
    escaped_target = store.reserve_derivative(CUSTOMER_A_ID, escaped_job, 1)
    escaped_directory = escaped_target.artifact_path.parent
    escaped_directory.rmdir()
    escaped_directory.symlink_to(foreign_artifact.parent, target_is_directory=True)

    with pytest.raises(AssetContentStoreError, match="unsafe_asset_store_path"):
        store.create_derivative(
            escaped_target,
            _normalized_payload(b"must not be written"),
        )
    assert store.cleanup_derivatives(CUSTOMER_A_ID, escaped_job) == (
        "asset_store_cleanup_directory_mismatch",
    )
    assert foreign_artifact.read_bytes() == b"customer-b-owned"


def test_synthetic_store_cleanup_does_not_delete_replacement_file(
    tmp_path: Path,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    artifact = store.create_derivative(
        target, _normalized_payload(b"owned staging")
    ).output_path
    artifact.unlink()
    artifact.write_bytes(b"replacement not created by this store call")

    cleanup_errors = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert cleanup_errors == ("asset_store_cleanup_unowned_entries",)
    assert artifact.read_bytes() == b"replacement not created by this store call"


def test_synthetic_store_rejects_configured_root_symlink(tmp_path: Path) -> None:
    real_root = tmp_path / "real-store"
    real_root.mkdir()
    configured_root = tmp_path / "store-link"
    configured_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(AssetContentStoreError, match="unsafe_asset_store_root"):
        LocalSyntheticAssetStore(configured_root)


def test_synthetic_store_rejects_preexisting_private_namespace(
    tmp_path: Path,
) -> None:
    configured_root = tmp_path / "preexisting-store"
    configured_root.mkdir(mode=0o700)
    sentinel = configured_root / "external.keep"
    sentinel.write_bytes(b"pre-existing namespace")

    with pytest.raises(AssetContentStoreError, match="unsafe_asset_store_root"):
        LocalSyntheticAssetStore(configured_root)

    assert sentinel.read_bytes() == b"pre-existing namespace"


def test_synthetic_store_quarantine_race_preserves_replacement_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    store.create_derivative(target, _normalized_payload(b"owned candidate"))
    owned_backup = tmp_path / "owned-backup"
    replacement_file: Path | None = None

    def replace_after_identity_check(job_directory: Path) -> None:
        nonlocal replacement_file
        job_directory.rename(owned_backup)
        job_directory.mkdir()
        replacement_file = job_directory / "external.keep"
        replacement_file.write_bytes(b"replacement survives")

    monkeypatch.setattr(
        real_photos_module, "_before_cleanup_validation", replace_after_identity_check
    )

    cleanup_errors = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert cleanup_errors == ("asset_store_cleanup_directory_mismatch",)
    assert replacement_file is not None
    assert replacement_file.read_bytes() == b"replacement survives"
    assert owned_backup.is_dir()


def test_synthetic_store_cleanup_preserves_replacement_after_identity_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    store.create_derivative(target, _normalized_payload(b"owned candidate"))
    displaced = tmp_path / "displaced-quarantined-candidate"
    replacement: Path | None = None

    def replace_after_scan(quarantine: Path) -> None:
        nonlocal replacement
        candidate = quarantine / "candidate-1.jpg"
        candidate.rename(displaced)
        candidate.write_bytes(b"external replacement after scan")
        replacement = candidate

    monkeypatch.setattr(
        real_photos_module, "_before_cleanup_commit", replace_after_scan
    )

    cleanup_errors = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert cleanup_errors == ("asset_store_cleanup_ownership_mismatch",)
    assert replacement is not None
    assert replacement.read_bytes() == b"external replacement after scan"
    assert displaced.read_bytes() == b"owned candidate"
    markers = list((tmp_path / "store").glob(".abandoned-*.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text()) == {
        "customer_id": str(CUSTOMER_A_ID),
        "job_id": str(job_id),
        "reason": "asset_store_cleanup_ownership_mismatch",
        "state": "abandoned",
    }


def test_synthetic_store_cleanup_rejects_job_directory_move_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    store.create_derivative(target, _normalized_payload(b"owned candidate"))
    displaced = tmp_path / "externally-displaced-cleanup-directory"
    replacement_sentinel: Path | None = None

    def move_directory_after_validation(job_directory: Path) -> None:
        nonlocal replacement_sentinel
        job_directory.rename(displaced)
        job_directory.mkdir()
        replacement_sentinel = job_directory / "external.keep"
        replacement_sentinel.write_bytes(b"replacement directory survives")

    monkeypatch.setattr(
        real_photos_module,
        "_before_cleanup_commit",
        move_directory_after_validation,
    )

    cleanup_errors = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert cleanup_errors == ("asset_store_cleanup_directory_mismatch",)
    assert replacement_sentinel is not None
    assert replacement_sentinel.read_bytes() == b"replacement directory survives"
    assert (displaced / "candidate-1.jpg").read_bytes() == b"owned candidate"


def test_synthetic_store_cleanup_does_not_swallow_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "store")
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    store.create_derivative(target, _normalized_payload(b"owned candidate"))

    def interrupt_before_cleanup_commit(job_directory: Path) -> None:
        del job_directory
        raise KeyboardInterrupt

    monkeypatch.setattr(
        real_photos_module,
        "_before_cleanup_commit",
        interrupt_before_cleanup_commit,
    )

    with pytest.raises(KeyboardInterrupt):
        store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert target.artifact_path.read_bytes() == b"owned candidate"
    assert store.verify_derivative(
        CUSTOMER_A_ID,
        job_id,
        1,
        sha256(b"owned candidate").hexdigest(),
    )


@pytest.mark.parametrize(
    "interruption",
    [KeyboardInterrupt(), SystemExit(91)],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_synthetic_store_cleanup_records_abandonment_after_first_real_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    first = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    second = store.reserve_derivative(CUSTOMER_A_ID, job_id, 2)
    store.create_derivative(first, _normalized_payload(b"first owned candidate"))
    store.create_derivative(second, _normalized_payload(b"second owned candidate"))
    real_unlink = os.unlink
    unlink_count = 0

    def unlink_then_interrupt(
        name: str | bytes,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal unlink_count
        real_unlink(name, dir_fd=dir_fd)
        unlink_count += 1
        if unlink_count == 1:
            raise interruption

    monkeypatch.setattr(os, "unlink", unlink_then_interrupt)

    with pytest.raises(type(interruption)):
        store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    monkeypatch.setattr(os, "unlink", real_unlink)
    assert unlink_count == 1
    assert not first.artifact_path.exists()
    assert second.artifact_path.read_bytes() == b"second owned candidate"
    markers = list(root.glob(".abandoned-*.json"))
    assert len(markers) == 1
    assert list(root.glob(".abandoned-write-*.tmp")) == []
    assert json.loads(markers[0].read_text()) == {
        "customer_id": str(CUSTOMER_A_ID),
        "job_id": str(job_id),
        "reason": "asset_store_cleanup_interrupted",
        "state": "abandoned",
    }
    assert store.cleanup_derivatives(CUSTOMER_A_ID, job_id) == (
        "asset_store_cleanup_interrupted",
    )


def test_cleanup_marker_base_exception_keeps_job_owned_for_failed_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    first = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    second = store.reserve_derivative(CUSTOMER_A_ID, job_id, 2)
    store.create_derivative(first, _normalized_payload(b"first owned candidate"))
    store.create_derivative(second, _normalized_payload(b"second owned candidate"))
    job = store._jobs[(CUSTOMER_A_ID, job_id)]
    expected_owned_files = dict(job.owned_files)
    real_unlink = os.unlink
    real_record = store._record_abandoned_state
    unlink_count = 0

    def unlink_first_then_fail(
        name: str | bytes,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal unlink_count
        unlink_count += 1
        if unlink_count == 1:
            real_unlink(name, dir_fd=dir_fd)
            return
        raise OSError("synthetic second unlink failure")

    def interrupt_marker(
        customer_id: UUID, abandoned_job_id: UUID, code: str
    ) -> bool:
        del customer_id, abandoned_job_id, code
        raise SystemExit(92)

    monkeypatch.setattr(os, "unlink", unlink_first_then_fail)
    monkeypatch.setattr(store, "_record_abandoned_state", interrupt_marker)

    with pytest.raises(SystemExit, match="92"):
        store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert store._jobs[(CUSTOMER_A_ID, job_id)] is job
    assert job.owned_files == expected_owned_files
    assert list(root.glob(".abandoned-*.json")) == []

    monkeypatch.setattr(os, "unlink", real_unlink)
    monkeypatch.setattr(store, "_record_abandoned_state", real_record)
    replay = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)
    assert replay != ()
    assert len(list(root.glob(".abandoned-*.json"))) == 1


def test_cleanup_marker_write_error_keeps_job_ownership_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    first = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    second = store.reserve_derivative(CUSTOMER_A_ID, job_id, 2)
    store.create_derivative(first, _normalized_payload(b"first owned candidate"))
    store.create_derivative(second, _normalized_payload(b"second owned candidate"))
    job = store._jobs[(CUSTOMER_A_ID, job_id)]
    expected_owned_files = dict(job.owned_files)
    real_unlink = os.unlink
    real_open_exclusive_file = real_photos_module._open_exclusive_file_at
    unlink_count = 0

    def unlink_first_then_fail(
        name: str | bytes,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal unlink_count
        unlink_count += 1
        if unlink_count == 1:
            real_unlink(name, dir_fd=dir_fd)
            return
        raise OSError("synthetic second unlink failure")

    def fail_marker_open(directory_fd: int, name: str) -> int:
        del directory_fd, name
        raise OSError("synthetic marker write failure")

    monkeypatch.setattr(os, "unlink", unlink_first_then_fail)
    monkeypatch.setattr(
        real_photos_module, "_open_exclusive_file_at", fail_marker_open
    )

    assert store.cleanup_derivatives(CUSTOMER_A_ID, job_id) == (
        "asset_store_cleanup_abandonment_untracked",
    )
    assert store._jobs[(CUSTOMER_A_ID, job_id)] is job
    assert job.owned_files == expected_owned_files

    monkeypatch.setattr(os, "unlink", real_unlink)
    monkeypatch.setattr(
        real_photos_module, "_open_exclusive_file_at", real_open_exclusive_file
    )
    replay = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)
    assert replay != ()
    assert len(list(root.glob(".abandoned-*.json"))) == 1


def test_synthetic_store_normal_cleanup_has_no_job_or_sibling_quarantine(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    store.create_derivative(target, _normalized_payload(b"owned candidate"))

    assert store.cleanup_derivatives(CUSTOMER_A_ID, job_id) == ()

    assert _artifact_entries(root) == []
    assert not any(
        path.name.startswith(".opc-style-quarantine-")
        for path in tmp_path.iterdir()
    )


def test_synthetic_store_rejects_mkdtemp_directory_swap_before_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    replacement: Path | None = None
    displaced = tmp_path / "displaced-owned-directory"

    def swap_created_directory(directory: Path) -> None:
        nonlocal replacement
        directory.rename(displaced)
        directory.mkdir()
        replacement = directory / "external.keep"
        replacement.write_bytes(b"directory swapped before identity capture")

    monkeypatch.setattr(
        real_photos_module,
        "_after_job_directory_created",
        swap_created_directory,
        raising=False,
    )

    with pytest.raises(AssetContentStoreError, match="unsafe_asset_store_path"):
        store.reserve_derivative(CUSTOMER_A_ID, uuid4(), 1)

    assert replacement is not None
    assert replacement.read_bytes() == b"directory swapped before identity capture"
    assert displaced.is_dir()


def test_job_directory_validation_and_marker_write_error_remains_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    real_open_exclusive_file = real_photos_module._open_exclusive_file_at

    def fail_after_identity_capture(job_directory: Path) -> None:
        del job_directory
        raise OSError("synthetic post-identity validation failure")

    def fail_marker_open(directory_fd: int, name: str) -> int:
        del directory_fd, name
        raise OSError("synthetic marker open failure")

    monkeypatch.setattr(
        real_photos_module,
        "_after_job_directory_created",
        fail_after_identity_capture,
    )
    monkeypatch.setattr(
        real_photos_module,
        "_open_exclusive_file_at",
        fail_marker_open,
    )

    with pytest.raises(AssetContentStoreError, match="unsafe_asset_store_path"):
        store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)

    job_directories = list(root.glob("derivative-*"))
    assert len(job_directories) == 1
    assert (CUSTOMER_A_ID, job_id) in store._jobs

    monkeypatch.setattr(
        real_photos_module,
        "_after_job_directory_created",
        lambda job_directory: None,
    )
    monkeypatch.setattr(
        real_photos_module,
        "_open_exclusive_file_at",
        real_open_exclusive_file,
    )
    replay = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert replay == ()
    assert not job_directories[0].exists()


def test_job_directory_validation_and_marker_base_exception_remains_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    real_record = store._record_abandoned_state

    def fail_after_identity_capture(job_directory: Path) -> None:
        del job_directory
        raise OSError("synthetic post-identity validation failure")

    def interrupt_marker(
        customer_id: UUID, abandoned_job_id: UUID, code: str
    ) -> bool:
        del customer_id, abandoned_job_id, code
        raise SystemExit(93)

    monkeypatch.setattr(
        real_photos_module,
        "_after_job_directory_created",
        fail_after_identity_capture,
    )
    monkeypatch.setattr(store, "_record_abandoned_state", interrupt_marker)

    with pytest.raises(SystemExit, match="93"):
        store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)

    job_directories = list(root.glob("derivative-*"))
    assert len(job_directories) == 1
    assert (CUSTOMER_A_ID, job_id) in store._jobs

    monkeypatch.setattr(
        real_photos_module,
        "_after_job_directory_created",
        lambda job_directory: None,
    )
    monkeypatch.setattr(store, "_record_abandoned_state", real_record)
    replay = store.cleanup_derivatives(CUSTOMER_A_ID, job_id)

    assert replay == ()
    assert not job_directories[0].exists()


def test_synthetic_store_uses_owned_directory_fd_if_path_swaps_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    store = LocalSyntheticAssetStore(root)
    job_id = uuid4()
    target = store.reserve_derivative(CUSTOMER_A_ID, job_id, 1)
    displaced = tmp_path / "displaced-job-before-open"
    replacement_sentinel: Path | None = None

    def swap_after_verification(job_directory: Path) -> None:
        nonlocal replacement_sentinel
        job_directory.rename(displaced)
        job_directory.mkdir()
        replacement_sentinel = job_directory / "external.keep"
        replacement_sentinel.write_bytes(b"external directory")

    monkeypatch.setattr(
        real_photos_module,
        "_after_job_directory_verification_before_publish",
        swap_after_verification,
        raising=False,
    )

    with pytest.raises(AssetContentStoreError, match="unsafe_asset_store_path"):
        store.create_derivative(target, _normalized_payload(b"must stay owned"))

    assert replacement_sentinel is not None
    assert replacement_sentinel.read_bytes() == b"external directory"
    assert not (target.artifact_path.parent / "candidate-1.jpg").exists()
    assert (displaced / "candidate-1.jpg").read_bytes() == b"must stay owned"


def test_store_fsync_failure_leaves_failed_job_with_zero_artifacts(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    asset_factory: Callable[..., tuple[ProductAsset, Path]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset, _ = asset_factory()
    def fail_fsync(file_descriptor: int) -> None:
        del file_descriptor
        raise OSError("synthetic store fsync failure")

    monkeypatch.setattr(real_photos_module.os, "fsync", fail_fsync)

    with api_session_factory() as session:
        result = RealPhotoOutputService(session, content_store).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v1",
            idempotency_key="store-fsync-failure",
        )

    assert result.job.status == "generation_failed"
    assert result.candidates == ()
    assert content_store.list_derivatives(CUSTOMER_A_ID, result.job.id) == ()
    assert _artifact_entries(tmp_path / "synthetic-assets") == []
    with api_session_factory() as session:
        assert session.query(ImageCandidate).count() == 0


def _normalized_payload(payload: bytes) -> Any:
    from opc_style.domain.image_normalizer import NormalizedPayload

    return NormalizedPayload(
        content=payload,
        content_sha256=sha256(payload).hexdigest(),
        mime_type="image/jpeg",
        width=1,
        height=1,
        metadata={"synthetic": True},
    )
