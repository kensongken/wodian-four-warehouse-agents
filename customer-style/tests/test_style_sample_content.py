"""Regression coverage for immutable, authorized historical style content."""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.main import create_app
from opc_style.models import Base, Customer, StyleSample
from opc_style.repositories import StyleRepository
from opc_style.services.asset_store_recovery import AssetStoreRecoveryService
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.services.samples import StyleSampleContentSelection, StyleSampleService
from opc_style.services.styles import (
    InsufficientStyleSamplesError,
    MainStyleCardService,
)

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def jpeg_bytes() -> bytes:
    """A hand-built valid JPEG fixture; corrupt bytes must not pass as media."""
    image = BytesIO()
    Image.new("RGB", (48, 32), (20, 90, 160)).save(image, format="JPEG")
    return image.getvalue()


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    from opc_style.db import create_database_engine

    engine = create_database_engine(f"sqlite:///{tmp_path / 'style-samples.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(
    api_session_factory: sessionmaker[Session], tmp_path: Path
) -> Generator[TestClient, None, None]:
    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with LocalSyntheticAssetStore(tmp_path / "style-sample-store") as store:
        app.state.classification_bridge_store = store
        with TestClient(app) as test_client:
            yield test_client


def authorize_sample_content(
    client: TestClient,
    *,
    customer_id: UUID = CUSTOMER_A_ID,
    photo_id: str = "history-1",
    body: bytes,
    claimed_sha256: str | None = None,
    consent_granted: str = "true",
    consent_provenance: str = "customer_style_history_opt_in",
    consent_text_version: str = "style-consent-v1",
) -> object:
    return client.post(
        "/v1/style-samples/content",
        content=body,
        headers={
            "X-Customer-Id": str(customer_id),
            "X-Photo-Id": photo_id,
            "X-Content-Sha256": claimed_sha256 or sha256(body).hexdigest(),
            "X-Mime-Type": "image/jpeg",
            "X-Consent-Granted": consent_granted,
            "X-Consent-Provenance": consent_provenance,
            "X-Consent-Text-Version": consent_text_version,
        },
    )


def test_authorized_sample_seals_verified_content_and_never_returns_a_path(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    jpeg_bytes: bytes,
) -> None:
    """Removing content verification or exposing a path must break this contract."""
    response = authorize_sample_content(client, body=jpeg_bytes)

    assert response.status_code == 201
    payload = response.json()
    assert payload["content_sha256"] == sha256(jpeg_bytes).hexdigest()
    assert payload["mime_type"] == "image/jpeg"
    assert payload["width"] == 48
    assert payload["height"] == 32
    assert "path" not in payload
    assert "storage" not in payload
    with api_session_factory() as session:
        stored = session.get(StyleSample, UUID(payload["id"]))
    assert stored is not None
    assert stored.consent_text_version == "style-consent-v1"
    assert stored.content_sha256 == sha256(jpeg_bytes).hexdigest()
    assert stored.mime_type == "image/jpeg"
    assert (stored.width, stored.height) == (48, 32)
    assert stored.asset_store_namespace == (
        client.app.state.classification_bridge_store.store_namespace
    )


def test_unconsented_or_hash_mismatched_sample_writes_nothing(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    jpeg_bytes: bytes,
) -> None:
    """Bypassing consent or hash verification must create neither row nor media."""
    store = client.app.state.classification_bridge_store

    unconsented = authorize_sample_content(
        client, body=jpeg_bytes, consent_granted="false"
    )
    mismatched = authorize_sample_content(
        client, body=jpeg_bytes, claimed_sha256="0" * 64
    )

    assert unconsented.status_code == 422
    assert mismatched.status_code == 409
    with api_session_factory() as session:
        assert session.query(StyleSample).count() == 0
    assert store.sample_content_count == 0


def test_same_authorized_content_replays_idempotently(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    """Changing a retry into a second write or conflict must break this guarantee."""
    first = authorize_sample_content(client, body=jpeg_bytes)
    replay = authorize_sample_content(client, body=jpeg_bytes)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert client.app.state.classification_bridge_store.sample_content_count == 1


def test_replay_rejects_changed_consent_text_version(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    """Dropping legal-version comparison must never turn a changed agreement into a replay."""
    assert authorize_sample_content(client, body=jpeg_bytes).status_code == 201

    changed_version = authorize_sample_content(
        client, body=jpeg_bytes, consent_text_version="style-consent-v2"
    )

    assert changed_version.status_code == 409
    assert changed_version.json()["error"]["code"] == "style_sample_consent_version_conflict"


def test_replay_rejects_changed_consent_provenance(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    """Changing the source of authority must not silently reuse a sealed sample."""
    assert authorize_sample_content(client, body=jpeg_bytes).status_code == 201

    changed_provenance = authorize_sample_content(
        client, body=jpeg_bytes, consent_provenance="operator_assertion"
    )

    assert changed_provenance.status_code == 409
    assert (
        changed_provenance.json()["error"]["code"]
        == "style_sample_consent_provenance_conflict"
    )


def test_replay_fails_closed_when_store_content_is_absent(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    """Removing store-owned bytes must not let the database row impersonate content."""
    created = authorize_sample_content(client, body=jpeg_bytes)
    sample_id = UUID(created.json()["id"])
    store = client.app.state.classification_bridge_store
    store.discard_style_sample_content(CUSTOMER_A_ID, sample_id)

    replay = authorize_sample_content(client, body=jpeg_bytes)

    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "style_sample_content_unavailable"


def test_replay_fails_closed_when_store_namespace_changes(
    client: TestClient, jpeg_bytes: bytes, tmp_path: Path
) -> None:
    """A row sealed in one private namespace cannot be replayed through another."""
    assert authorize_sample_content(client, body=jpeg_bytes).status_code == 201
    original_store = client.app.state.classification_bridge_store
    with LocalSyntheticAssetStore(tmp_path / "replacement-style-sample-store") as store:
        client.app.state.classification_bridge_store = store
        replay = authorize_sample_content(client, body=jpeg_bytes)
    client.app.state.classification_bridge_store = original_store

    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "style_sample_content_store_mismatch"


def test_recovered_sample_content_replays_through_its_verified_binding(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    jpeg_bytes: bytes,
    tmp_path: Path,
) -> None:
    """Ignoring a valid recovery binding must not block the same immutable upload."""
    digest = sha256(jpeg_bytes).hexdigest()
    sample_id = uuid4()
    source_root = tmp_path / "legacy-style-sample-store"
    source_root.mkdir(mode=0o700)
    with (
        LocalSyntheticAssetStore(source_root, reopen=True) as source,
        api_session_factory() as session,
    ):
            session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
            session.add(
                StyleSample(
                    id=sample_id,
                    customer_id=CUSTOMER_A_ID,
                    photo_id=f"history-{digest}",
                    authorized_at=datetime.now(UTC),
                    consent_provenance="customer_style_history_opt_in",
                    consent_text_version="style-consent-v1",
                    content_sha256=digest,
                    mime_type="image/jpeg",
                    width=48,
                    height=32,
                    asset_store_namespace=source.store_namespace,
                )
            )
            source.persist_style_sample_content(
                CUSTOMER_A_ID, sample_id, jpeg_bytes, digest
            )
            session.commit()
            AssetStoreRecoveryService(session).recover(
                CUSTOMER_A_ID,
                source.store_namespace,
                source,
                client.app.state.classification_bridge_store,
                "verified-replay-regression",
            )

    replay = authorize_sample_content(
        client,
        photo_id=f"history-{digest}",
        body=jpeg_bytes,
    )

    assert replay.status_code == 200
    assert replay.json()["id"] == str(sample_id)


def test_contentless_legacy_samples_never_count_as_ready_or_reach_analysis(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
) -> None:
    """Counting consent-only rows would feed analysis references with no immutable bytes."""
    for number in range(10):
        response = client.post(
            "/v1/style-samples",
            headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
            json={
                "photo_id": f"legacy-{number}",
                "consent_text_version": "style-consent-v1",
            },
        )
        assert response.status_code == 201

    readiness = client.get(
        "/v1/style-samples/readiness", headers={"X-Customer-Id": str(CUSTOMER_A_ID)}
    )
    with api_session_factory() as session:
        service = MainStyleCardService(session)
        with pytest.raises(InsufficientStyleSamplesError) as error:
            service.generate(CUSTOMER_A_ID)

    assert readiness.json()["active_sample_count"] == 0
    assert error.value.readiness.active_sample_count == 0


def test_blank_legacy_consent_provenance_is_not_a_complete_content_seal(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
) -> None:
    """A legal seal needs an authority source as well as content and its text version."""
    created = client.post(
        "/v1/style-samples",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
        json={"photo_id": "legacy-customer-anchor", "consent_text_version": "v1"},
    )
    assert created.status_code == 201
    with api_session_factory() as session:
        sample = StyleSample(
            customer_id=CUSTOMER_A_ID,
            photo_id="legacy-blank-provenance",
            authorized_at=datetime.now(UTC),
            consent_provenance="",
            consent_text_version="style-consent-v1",
            content_sha256="a" * 64,
            mime_type="image/jpeg",
            width=10,
            height=10,
            asset_store_namespace="synthetic-test-store",
        )
        session.add(sample)
        session.commit()

    readiness = client.get(
        "/v1/style-samples/readiness", headers={"X-Customer-Id": str(CUSTOMER_A_ID)}
    )

    assert readiness.json()["active_sample_count"] == 0


def test_concurrent_same_content_authorization_is_idempotent(
    api_session_factory: sessionmaker[Session],
    jpeg_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A duplicate scan race must converge on one sealed customer sample."""
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.commit()
    barrier = Barrier(2)
    original_get = StyleRepository.get_sample_by_photo

    def gate_missing_sample_read(
        repository: StyleRepository, customer_id: UUID, photo_id: str
    ) -> StyleSample | None:
        sample = original_get(repository, customer_id, photo_id)
        if sample is None:
            barrier.wait(timeout=5)
        return sample

    monkeypatch.setattr(StyleRepository, "get_sample_by_photo", gate_missing_sample_read)
    with LocalSyntheticAssetStore(tmp_path / "concurrent-content-store") as store:

        def authorize() -> StyleSampleContentSelection:
            with api_session_factory() as session:
                return StyleSampleService(session, store).authorize_content(
                    CUSTOMER_A_ID,
                    photo_id="concurrent-history-1",
                    content=jpeg_bytes,
                    claimed_sha256=sha256(jpeg_bytes).hexdigest(),
                    mime_type="image/jpeg",
                    consent_provenance="customer_style_history_opt_in",
                    consent_text_version="style-consent-v1",
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            selections = list(executor.map(lambda _: authorize(), range(2)))

        assert len({selection.sample_id for selection in selections}) == 1
        assert store.sample_content_count == 1
    with api_session_factory() as session:
        assert session.query(StyleSample).count() == 1


def test_content_authorization_is_customer_isolated_and_withdrawn_samples_fail_closed(
    client: TestClient, jpeg_bytes: bytes
) -> None:
    """Dropping customer scope or allowing a withdrawn replay must be observable."""
    created = authorize_sample_content(client, body=jpeg_bytes)
    assert created.status_code == 201

    sample_id = created.json()["id"]
    withdrawn = client.post(
        f"/v1/style-samples/{sample_id}/withdraw",
        headers={"X-Customer-Id": str(CUSTOMER_A_ID)},
    )
    foreign = authorize_sample_content(
        client, customer_id=CUSTOMER_B_ID, body=jpeg_bytes
    )
    replay = authorize_sample_content(client, body=jpeg_bytes)

    assert withdrawn.status_code == 200
    assert foreign.status_code == 201
    assert foreign.json()["id"] != sample_id
    assert replay.status_code == 409
