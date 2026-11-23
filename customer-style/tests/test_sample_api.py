from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from opc_style.db import create_database_engine
from opc_style.domain.errors import DomainRuleError
from opc_style.main import create_app
from opc_style.models import AuditEvent, Base, Customer, StyleSample
from opc_style.repositories import StyleRepository
from opc_style.services.samples import StyleSampleAuthorization, StyleSampleService
from opc_style.services.styles import (
    InsufficientStyleSamplesError,
    MainStyleCardService,
)

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'sample-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(
    api_session_factory: sessionmaker[Session],
) -> Generator[TestClient, None, None]:
    from opc_style.api.dependencies import get_session

    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client


def authorize_sample(
    client: TestClient,
    headers: dict[str, str],
    photo_id: str,
    *,
    expected_status: int = 201,
) -> dict[str, str]:
    response = client.post(
        "/v1/style-samples",
        headers=headers,
        json={"photo_id": photo_id, "consent_text_version": "style-consent-v1"},
    )

    assert response.status_code == expected_status
    return response.json()


def gate_concurrent_missing_sample_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make two authorization attempts observe the same missing-photo state."""
    barrier = Barrier(2)
    original_get = StyleRepository.get_sample_by_photo

    def gated_get(
        repository: StyleRepository, customer_id: UUID, photo_id: str
    ) -> StyleSample | None:
        sample = original_get(repository, customer_id, photo_id)
        if sample is None:
            barrier.wait(timeout=5)
        return sample

    monkeypatch.setattr(StyleRepository, "get_sample_by_photo", gated_get)


def test_explicit_authorization_is_required(client: TestClient) -> None:
    readiness = client.get("/v1/style-samples/readiness", headers=CUSTOMER_A).json()

    assert readiness["active_sample_count"] == 0
    assert readiness["samples_needed"] == 10
    assert readiness["can_generate"] is False


def test_local_synthetic_samples_do_not_make_real_style_ready(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    """Historical demo rows must not unlock real customer style generation."""
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.add_all(
            StyleSample(
                id=uuid4(),
                customer_id=CUSTOMER_A_ID,
                photo_id=f"local-demo-{index}",
                authorized_at=datetime.now(UTC),
                consent_provenance="local_synthetic_acceptance",
                consent_text_version="local-demo-v1",
                content_sha256=f"{index:064x}",
                mime_type="image/jpeg",
                width=10,
                height=12,
                asset_store_namespace="synthetic-only",
            )
            for index in range(10)
        )
        session.commit()

    readiness = client.get("/v1/style-samples/readiness", headers=CUSTOMER_A)
    assert readiness.status_code == 200
    assert readiness.json()["active_sample_count"] == 0
    assert readiness.json()["can_generate"] is False

    with (
        api_session_factory() as session,
        pytest.raises(InsufficientStyleSamplesError),
    ):
        MainStyleCardService(session).generate(CUSTOMER_A_ID)


def test_authorization_uses_header_identity_and_records_audit_evidence(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/v1/style-samples",
        headers=CUSTOMER_A,
        json={
            "photo_id": "photo-001",
            "consent_text_version": "style-consent-v1",
            "customer_id": str(CUSTOMER_B_ID),
        },
    )

    assert response.status_code == 422

    response = client.post(
        "/v1/style-samples",
        headers=CUSTOMER_A,
        json={"photo_id": "photo-001", "consent_text_version": "style-consent-v1"},
    )

    assert response.status_code == 201
    sample = response.json()
    assert sample["photo_id"] == "photo-001"
    assert sample["status"] == "active"
    assert sample["authorized_at"] is not None

    with api_session_factory() as session:
        stored_sample = session.get(StyleSample, UUID(sample["id"]))
        audit_events = session.query(AuditEvent).all()

    assert stored_sample is not None
    assert stored_sample.customer_id == CUSTOMER_A_ID
    assert stored_sample.consent_provenance == "style-consent-v1"
    assert [(event.customer_id, event.event_type) for event in audit_events] == [
        (CUSTOMER_A_ID, "style_sample.authorized")
    ]


def test_repeated_active_authorization_is_idempotent(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    first_sample = authorize_sample(client, CUSTOMER_A, "photo-001")
    second_sample = authorize_sample(
        client, CUSTOMER_A, "photo-001", expected_status=200
    )

    assert second_sample["id"] == first_sample["id"]
    assert second_sample["authorized_at"] == first_sample["authorized_at"]

    with api_session_factory() as session:
        assert session.query(StyleSample).count() == 1
        assert session.query(AuditEvent).count() == 1


def test_concurrent_same_version_authorization_is_atomic_at_service_layer(
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.commit()
    gate_concurrent_missing_sample_reads(monkeypatch)

    def authorize() -> StyleSampleAuthorization:
        with api_session_factory() as session:
            return StyleSampleService(session).authorize(
                CUSTOMER_A_ID,
                photo_id="concurrent-same-version",
                consent_text_version="style-consent-v1",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        authorizations = list(executor.map(lambda _: authorize(), range(2)))

    assert sorted(result.created for result in authorizations) == [False, True]
    assert len({result.sample.id for result in authorizations}) == 1
    with api_session_factory() as session:
        assert session.query(StyleSample).count() == 1
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "style_sample.authorized")
            .count()
            == 1
        )


def test_concurrent_different_version_authorization_conflicts_at_service_layer(
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.commit()
    gate_concurrent_missing_sample_reads(monkeypatch)

    def authorize(consent_text_version: str) -> StyleSampleAuthorization | str:
        with api_session_factory() as session:
            try:
                return StyleSampleService(session).authorize(
                    CUSTOMER_A_ID,
                    photo_id="concurrent-different-version",
                    consent_text_version=consent_text_version,
                )
            except DomainRuleError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(authorize, ["style-consent-v1", "style-consent-v2"])
        )

    assert sum(isinstance(result, StyleSampleAuthorization) for result in results) == 1
    assert "style_sample_consent_version_conflict" in results
    with api_session_factory() as session:
        assert session.query(StyleSample).count() == 1
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "style_sample.authorized")
            .count()
            == 1
        )


def test_concurrent_same_version_authorization_returns_201_and_200_at_api_layer(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.commit()
    gate_concurrent_missing_sample_reads(monkeypatch)

    def authorize() -> tuple[int, dict[str, object]]:
        response = client.post(
            "/v1/style-samples",
            headers=CUSTOMER_A,
            json={
                "photo_id": "api-concurrent-same-version",
                "consent_text_version": "style-consent-v1",
            },
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: authorize(), range(2)))

    assert sorted(status_code for status_code, _ in results) == [200, 201]
    assert len({payload["id"] for _, payload in results}) == 1
    with api_session_factory() as session:
        assert session.query(StyleSample).count() == 1
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "style_sample.authorized")
            .count()
            == 1
        )


def test_concurrent_different_version_authorization_returns_nonretryable_api_conflict(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.commit()
    gate_concurrent_missing_sample_reads(monkeypatch)

    def authorize(consent_text_version: str) -> tuple[int, dict[str, object]]:
        response = client.post(
            "/v1/style-samples",
            headers=CUSTOMER_A,
            json={
                "photo_id": "api-concurrent-different-version",
                "consent_text_version": consent_text_version,
            },
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(authorize, ["style-consent-v1", "style-consent-v2"])
        )

    assert sorted(status_code for status_code, _ in results) == [201, 409]
    conflict = next(payload for status_code, payload in results if status_code == 409)
    assert conflict == {
        "error": {
            "code": "style_sample_consent_version_conflict",
            "retryable": False,
        }
    }
    with api_session_factory() as session:
        assert session.query(StyleSample).count() == 1
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "style_sample.authorized")
            .count()
            == 1
        )


def test_withdrawal_excludes_sample_from_readiness_and_preserves_audit_evidence(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    sample = authorize_sample(client, CUSTOMER_A, "photo-001")

    response = client.post(
        f"/v1/style-samples/{sample['id']}/withdraw", headers=CUSTOMER_A
    )

    assert response.status_code == 200
    assert response.json()["status"] == "withdrawn"
    assert response.json()["withdrawn_at"] is not None
    readiness = client.get("/v1/style-samples/readiness", headers=CUSTOMER_A).json()
    assert readiness["active_sample_count"] == 0

    with api_session_factory() as session:
        stored_sample = session.get(StyleSample, UUID(sample["id"]))
        audit_events = session.query(AuditEvent).order_by(AuditEvent.occurred_at).all()

    assert stored_sample is not None
    assert stored_sample.withdrawn_at is not None
    assert [event.event_type for event in audit_events] == [
        "style_sample.authorized",
        "style_sample.withdrawn",
    ]


def test_other_customer_cannot_withdraw_sample(client: TestClient) -> None:
    authorized_sample = authorize_sample(client, CUSTOMER_A, "photo-001")

    response = client.post(
        f"/v1/style-samples/{authorized_sample['id']}/withdraw", headers=CUSTOMER_B
    )

    assert response.status_code == 404


def test_customer_header_must_be_a_uuid(client: TestClient) -> None:
    response = client.get(
        "/v1/style-samples/readiness", headers={"X-Customer-Id": "not-a-uuid"}
    )

    assert response.status_code == 422
