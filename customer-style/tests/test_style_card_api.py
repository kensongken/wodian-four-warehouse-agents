import sqlite3
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import opc_style.api.routes_styles as routes_styles_module
import opc_style.services.styles as styles_module
from opc_style.db import create_database_engine
from opc_style.domain.analyzer import SampleDescriptor, StyleAnalysis
from opc_style.domain.errors import DomainRuleError
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    Base,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.services.samples import StyleSampleService
from opc_style.services.styles import MainStyleCardService, SceneStyleCardService

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}


class ReplacementAnalyzer:
    """A deterministic test provider proving the service boundary is replaceable."""

    @property
    def label(self) -> str:
        return "replacement-test-provider-v2"

    @property
    def analysis_mode(self) -> str:
        return "replacement_test_mode"

    def analyze(self, samples: list[SampleDescriptor]) -> StyleAnalysis:
        return StyleAnalysis(
            tone=["replacement-tone"],
            palette=["replacement-palette"],
            lighting=["replacement-light"],
            composition=["replacement-composition"],
            backgrounds=["replacement-background"],
            people_and_props=["replacement-props"],
            material_treatment=["replacement-material"],
            typography=["replacement-type"],
            forbidden_elements=["replacement-forbidden"],
            evidence_photo_ids=sorted(sample.photo_id for sample in samples),
        )


class SelectiveEvidenceAnalyzer(ReplacementAnalyzer):
    """A replacement provider that cites only part of its authorized input."""

    def analyze(self, samples: list[SampleDescriptor]) -> StyleAnalysis:
        analysis = super().analyze(samples)
        return replace(
            analysis,
            evidence_photo_ids=sorted(sample.photo_id for sample in samples[:2]),
        )


class MaliciousEvidenceAnalyzer(ReplacementAnalyzer):
    """A provider double that returns caller-controlled invalid evidence ids."""

    def __init__(self, evidence_photo_ids: object) -> None:
        self._evidence_photo_ids = evidence_photo_ids

    def analyze(self, samples: list[SampleDescriptor]) -> StyleAnalysis:
        return replace(
            super().analyze(samples),
            evidence_photo_ids=self._evidence_photo_ids,
        )


class MaliciousStyleAnalyzer(ReplacementAnalyzer):
    """A provider double that returns caller-controlled invalid style content."""

    def __init__(self, tone: list[str]) -> None:
        self._tone = tone

    def analyze(self, samples: list[SampleDescriptor]) -> StyleAnalysis:
        return replace(super().analyze(samples), tone=self._tone)


class BarrierAnalyzer(ReplacementAnalyzer):
    """Release two generation attempts at the same allocation boundary."""

    def __init__(self, barrier: Barrier) -> None:
        self._barrier = barrier

    def analyze(self, samples: list[SampleDescriptor]) -> StyleAnalysis:
        analysis = super().analyze(samples)
        self._barrier.wait(timeout=5)
        return analysis


class PausedAnalyzer(ReplacementAnalyzer):
    """Pause generation after it has captured the analyzed sample set."""

    def __init__(self, started: Event, resume: Event) -> None:
        self._started = started
        self._resume = resume

    def analyze(self, samples: list[SampleDescriptor]) -> StyleAnalysis:
        analysis = super().analyze(samples)
        self._started.set()
        if not self._resume.wait(timeout=5):
            raise TimeoutError("paused analyzer was not resumed")
        return analysis


class ResidualConflictService(MainStyleCardService):
    """Force a real database uniqueness collision after normal serialization."""

    def _create_version(
        self, customer_id: UUID, card: StyleCard, content: dict[str, Any]
    ) -> StyleCardVersion:
        conflicting_version = StyleCardVersion(
            customer_id=customer_id,
            style_card_id=card.id,
            version=1,
            content=content,
        )
        self._session.add(conflicting_version)
        self._session.flush()
        return conflicting_version


class ResidualSceneVersionConflictService(SceneStyleCardService):
    """Force a scene-version uniqueness collision after normal serialization."""

    def _create_version(
        self, customer_id: UUID, card: StyleCard, content: dict[str, Any]
    ) -> StyleCardVersion:
        first = StyleCardVersion(
            customer_id=customer_id,
            style_card_id=card.id,
            version=1,
            content=content,
        )
        duplicate = StyleCardVersion(
            customer_id=customer_id,
            style_card_id=card.id,
            version=1,
            content=content,
        )
        self._session.add(first)
        self._session.flush()
        self._session.add(duplicate)
        self._session.flush()
        return duplicate


class UnrelatedOperationalErrorService(MainStyleCardService):
    """Raise a non-lock SQLite failure at the transaction boundary."""

    def _begin_serialized_sqlite_write(self) -> None:
        raise OperationalError(
            "BEGIN IMMEDIATE",
            {},
            sqlite3.OperationalError("disk I/O error"),
        )


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'style-card-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(
    api_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> Generator[TestClient, None, None]:
    from opc_style.api.dependencies import get_session

    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with LocalSyntheticAssetStore(tmp_path / "style-card-content-store") as store:
        app.state.classification_bridge_store = store
        with TestClient(app) as test_client:
            yield test_client


@pytest.fixture
def lock_contention_client(
    api_session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> Generator[TestClient, None, None]:
    from opc_style.api.dependencies import get_session

    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            session.execute(text("PRAGMA busy_timeout = 1"))
            yield session

    app.dependency_overrides[get_session] = override_session
    with LocalSyntheticAssetStore(tmp_path / "style-card-lock-content-store") as store:
        app.state.classification_bridge_store = store
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client


def authorize_samples(
    client: TestClient,
    headers: dict[str, str],
    count: int,
    *,
    prefix: str,
) -> list[dict[str, str]]:
    image = BytesIO()
    Image.new("RGB", (16, 12), (20, 90, 160)).save(image, format="JPEG")
    content = image.getvalue()
    samples = []
    for number in range(count):
        response = client.post(
            "/v1/style-samples/content",
            headers={
                **headers,
                "X-Photo-Id": f"{prefix}-{number:02d}",
                "X-Content-Sha256": sha256(content).hexdigest(),
                "X-Mime-Type": "image/jpeg",
                "X-Consent-Granted": "true",
                "X-Consent-Provenance": "synthetic-style-consent",
                "X-Consent-Text-Version": "synthetic-style-consent-v1",
            },
            content=content,
        )
        assert response.status_code == 201
        samples.append(response.json())
    return samples


def test_generation_requires_ten_active_authorized_samples(client: TestClient) -> None:
    authorize_samples(client, CUSTOMER_A, 9, prefix="customer-a-synthetic")

    response = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "insufficient_style_samples",
            "active_sample_count": 9,
            "samples_needed": 1,
        }
    }


def test_generation_accepts_a_replacement_analyzer_without_service_changes(
    session: Session,
    sample_factory: Callable[..., StyleSample],
) -> None:
    for number in range(10):
        sample_factory(CUSTOMER_A_ID, photo_id=f"replaceable-{number:02d}")

    snapshot = MainStyleCardService(session, analyzer=ReplacementAnalyzer()).generate(
        CUSTOMER_A_ID
    )

    assert snapshot.version.content["analyzer"] == "replacement-test-provider-v2"
    assert snapshot.version.content["analysis_mode"] == "replacement_test_mode"
    assert snapshot.version.content["tone"] == ["replacement-tone"]
    assert snapshot.version.content["evidence_photo_ids"] == [
        f"replaceable-{number:02d}" for number in range(10)
    ]


def test_generation_links_only_samples_cited_as_analysis_evidence(
    session: Session,
    sample_factory: Callable[..., StyleSample],
) -> None:
    for number in range(10):
        sample_factory(CUSTOMER_A_ID, photo_id=f"selective-{number:02d}")

    snapshot = MainStyleCardService(
        session, analyzer=SelectiveEvidenceAnalyzer()
    ).generate(CUSTOMER_A_ID)
    links = list(
        session.scalars(
            select(StyleCardSampleLink).where(
                StyleCardSampleLink.style_card_version_id == snapshot.version.id
            )
        )
    )
    linked_photo_ids = list(
        session.scalars(
            select(StyleSample.photo_id)
            .where(StyleSample.id.in_([link.style_sample_id for link in links]))
            .order_by(StyleSample.photo_id)
        )
    )

    assert linked_photo_ids == ["selective-00", "selective-01"]


@pytest.mark.parametrize(
    "malicious_evidence",
    [
        None,
        "malicious-00",
        7,
        [],
        [7],
        [["malicious-00"]],
        [{"photo_id": "malicious-00"}],
        [" "],
        ["malicious-00", "malicious-00"],
        ["other-customer-photo"],
    ],
)
def test_invalid_analyzer_evidence_is_rejected_before_any_durable_write(
    session: Session,
    sample_factory: Callable[..., StyleSample],
    malicious_evidence: object,
) -> None:
    for number in range(10):
        sample_factory(CUSTOMER_A_ID, photo_id=f"malicious-{number:02d}")
    sample_factory(CUSTOMER_B_ID, photo_id="other-customer-photo")

    with pytest.raises(
        DomainRuleError, match="invalid_analyzer_evidence"
    ) as error:
        MainStyleCardService(
            session,
            analyzer=MaliciousEvidenceAnalyzer(malicious_evidence),
        ).generate(CUSTOMER_A_ID)

    assert error.value.code == "invalid_analyzer_evidence"
    assert session.query(StyleCard).count() == 0
    assert session.query(StyleCardVersion).count() == 0
    assert session.query(StyleCardSampleLink).count() == 0
    assert session.query(AuditEvent).count() == 0


@pytest.mark.parametrize(
    "malicious_evidence",
    [None, "malicious-route-00", [["malicious-route-00"]]],
)
def test_invalid_analyzer_evidence_returns_stable_conflict_without_writes(
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    malicious_evidence: object,
    tmp_path: Path,
) -> None:
    from opc_style.api.dependencies import get_session

    malicious_analyzer = MaliciousEvidenceAnalyzer(malicious_evidence)
    monkeypatch.setattr(
        styles_module,
        "DeterministicLocalStyleAnalyzer",
        lambda: malicious_analyzer,
    )
    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with LocalSyntheticAssetStore(tmp_path / "malicious-evidence-store") as store:
        app.state.classification_bridge_store = store
        with TestClient(app, raise_server_exceptions=False) as test_client:
            authorize_samples(
                test_client,
                CUSTOMER_A,
                10,
                prefix="malicious-route",
            )
            response = test_client.post(
                "/v1/style-cards/main/generate", headers=CUSTOMER_A
            )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "invalid_analyzer_evidence"}}
    with api_session_factory() as session:
        assert session.query(StyleCard).count() == 0
        assert session.query(StyleCardVersion).count() == 0
        assert session.query(StyleCardSampleLink).count() == 0
        assert session.query(AuditEvent).count() == 10


@pytest.mark.parametrize(
    "malicious_tone",
    [
        ["   "],
        ["x" * 201],
        [f"item-{number}" for number in range(21)],
    ],
)
def test_invalid_analyzer_style_content_is_rejected_before_writes(
    session: Session,
    sample_factory: Callable[..., StyleSample],
    malicious_tone: list[str],
) -> None:
    for number in range(10):
        sample_factory(CUSTOMER_A_ID, photo_id=f"malicious-style-{number:02d}")

    with pytest.raises(
        DomainRuleError, match="invalid_analyzer_style_content"
    ) as error:
        MainStyleCardService(
            session,
            analyzer=MaliciousStyleAnalyzer(malicious_tone),
        ).generate(CUSTOMER_A_ID)

    assert error.value.code == "invalid_analyzer_style_content"
    assert session.query(StyleCard).count() == 0
    assert session.query(StyleCardVersion).count() == 0
    assert session.query(StyleCardSampleLink).count() == 0
    assert session.query(AuditEvent).count() == 0


def test_invalid_analyzer_style_content_returns_stable_conflict(
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opc_style.api.dependencies import get_session

    monkeypatch.setattr(
        styles_module,
        "DeterministicLocalStyleAnalyzer",
        lambda: MaliciousStyleAnalyzer([" "]),
    )
    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with LocalSyntheticAssetStore(tmp_path / "malicious-style-store") as store:
        app.state.classification_bridge_store = store
        with TestClient(app, raise_server_exceptions=False) as test_client:
            authorize_samples(
                test_client, CUSTOMER_A, 10, prefix="malicious-style-route"
            )
            response = test_client.post(
                "/v1/style-cards/main/generate", headers=CUSTOMER_A
            )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "invalid_analyzer_style_content"}
    }


def test_simultaneous_sqlite_generation_serializes_main_card_versions(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
) -> None:
    authorize_samples(client, CUSTOMER_A, 10, prefix="concurrent")
    analyzer = BarrierAnalyzer(Barrier(2))

    def generate_version() -> int:
        with api_session_factory() as session:
            snapshot = MainStyleCardService(session, analyzer=analyzer).generate(
                CUSTOMER_A_ID
            )
            return snapshot.version.version

    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = sorted(executor.map(lambda _: generate_version(), range(2)))

    assert versions == [1, 2]
    with api_session_factory() as session:
        assert session.query(StyleCard).count() == 1
        assert session.query(StyleCardVersion).count() == 2
        assert session.query(AuditEvent).filter(
            AuditEvent.event_type == "style_card.main.generated"
        ).count() == 2


def test_withdrawal_during_paused_analysis_returns_retryable_conflict_without_artifacts(
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opc_style.api.dependencies import get_session

    started = Event()
    resume = Event()
    analyzer = PausedAnalyzer(started, resume)
    monkeypatch.setattr(
        styles_module,
        "DeterministicLocalStyleAnalyzer",
        lambda: analyzer,
    )
    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with LocalSyntheticAssetStore(tmp_path / "withdrawal-analysis-store") as store:
        app.state.classification_bridge_store = store
        with TestClient(app, raise_server_exceptions=False) as test_client:
            samples = authorize_samples(
                test_client,
                CUSTOMER_A,
                10,
                prefix="withdraw-during-analysis",
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    test_client.post,
                    "/v1/style-cards/main/generate",
                    headers=CUSTOMER_A,
                )
                assert started.wait(timeout=5)
                try:
                    with api_session_factory() as session:
                        withdrawn = StyleSampleService(session).withdraw(
                            CUSTOMER_A_ID, UUID(samples[0]["id"])
                        )
                        assert withdrawn.withdrawn_at is not None
                finally:
                    resume.set()
                response = future.result(timeout=5)

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "style_samples_changed", "retryable": True}
    }
    with api_session_factory() as session:
        assert session.query(StyleCard).count() == 0
        assert session.query(StyleCardVersion).count() == 0
        assert session.query(StyleCardSampleLink).count() == 0
        assert session.query(AuditEvent).filter(
            AuditEvent.event_type == "style_card.main.generated"
        ).count() == 0


def test_residual_unique_conflict_becomes_retryable_domain_error(
    session: Session,
    sample_factory: Callable[..., StyleSample],
) -> None:
    for number in range(10):
        sample_factory(CUSTOMER_A_ID, photo_id=f"residual-{number:02d}")
    MainStyleCardService(session).generate(CUSTOMER_A_ID)

    with pytest.raises(
        DomainRuleError, match="style_card_write_conflict"
    ) as error:
        ResidualConflictService(session).generate(CUSTOMER_A_ID)

    assert error.value.code == "style_card_write_conflict"
    assert session.query(StyleCard).count() == 1
    assert session.query(StyleCardVersion).count() == 1
    assert session.query(AuditEvent).filter(
        AuditEvent.event_type == "style_card.main.generated"
    ).count() == 1


def test_residual_unique_conflict_returns_retryable_http_conflict(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize_samples(client, CUSTOMER_A, 10, prefix="residual-route")
    first = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)
    assert first.status_code == 201
    monkeypatch.setattr(
        routes_styles_module,
        "MainStyleCardService",
        ResidualConflictService,
    )

    response = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "style_card_write_conflict", "retryable": True}
    }


def test_held_sqlite_write_lock_makes_generation_retryable_without_writes(
    lock_contention_client: TestClient,
    api_session_factory: sessionmaker[Session],
) -> None:
    authorize_samples(
        lock_contention_client,
        CUSTOMER_A,
        10,
        prefix="locked-generate",
    )
    with api_session_factory() as locker:
        locker.connection().exec_driver_sql("BEGIN IMMEDIATE")
        response = lock_contention_client.post(
            "/v1/style-cards/main/generate", headers=CUSTOMER_A
        )
        locker.rollback()

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "style_card_write_conflict", "retryable": True}
    }
    with api_session_factory() as session:
        assert session.query(StyleCard).count() == 0
        assert session.query(StyleCardVersion).count() == 0
        assert session.query(StyleCardSampleLink).count() == 0
        assert session.query(AuditEvent).filter(
            AuditEvent.event_type == "style_card.main.generated"
        ).count() == 0


def test_held_sqlite_write_lock_makes_edit_retryable_without_new_version(
    lock_contention_client: TestClient,
    api_session_factory: sessionmaker[Session],
) -> None:
    authorize_samples(
        lock_contention_client,
        CUSTOMER_A,
        10,
        prefix="locked-edit",
    )
    generated = lock_contention_client.post(
        "/v1/style-cards/main/generate", headers=CUSTOMER_A
    )
    assert generated.status_code == 201

    with api_session_factory() as locker:
        locker.connection().exec_driver_sql("BEGIN IMMEDIATE")
        response = lock_contention_client.patch(
            "/v1/style-cards/main",
            headers=CUSTOMER_A,
            json={"tone": ["锁等待后的新风格"]},
        )
        locker.rollback()

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "style_card_write_conflict", "retryable": True}
    }
    with api_session_factory() as session:
        assert session.query(StyleCard).count() == 1
        assert session.query(StyleCardVersion).count() == 1
        assert session.query(AuditEvent).filter(
            AuditEvent.event_type == "style_card.main.edited"
        ).count() == 0


def test_unrelated_operational_error_is_not_translated(
    session: Session,
    sample_factory: Callable[..., StyleSample],
) -> None:
    for number in range(10):
        sample_factory(CUSTOMER_A_ID, photo_id=f"unrelated-error-{number:02d}")

    with pytest.raises(OperationalError, match="disk I/O error"):
        UnrelatedOperationalErrorService(session).generate(CUSTOMER_A_ID)


def test_generation_is_local_test_only_and_links_only_customer_active_samples(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    own_samples = authorize_samples(
        client, CUSTOMER_A, 11, prefix="customer-a-synthetic"
    )
    withdrawn = own_samples.pop()
    withdrawal = client.post(
        f"/v1/style-samples/{withdrawn['id']}/withdraw", headers=CUSTOMER_A
    )
    assert withdrawal.status_code == 200
    authorize_samples(client, CUSTOMER_B, 10, prefix="customer-b-synthetic")

    response = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)

    assert response.status_code == 201
    card = response.json()
    assert card["version"] == 1
    assert card["kind"] == "main"
    assert card["status"] == "active"
    assert card["source"] == "analyzer"
    assert card["analyzer"] == "deterministic-local-v1"
    assert card["analysis_mode"] == "synthetic_metadata_test_only"
    assert card["stability"] == "initial"
    assert card["sample_count"] == 10
    assert card["evidence_photo_ids"] == [
        f"customer-a-synthetic-{number:02d}" for number in range(10)
    ]
    assert set(card) >= {
        "tone",
        "palette",
        "lighting",
        "composition",
        "backgrounds",
        "people_and_props",
        "material_treatment",
        "typography",
        "forbidden_elements",
    }

    with api_session_factory() as session:
        version = session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.id == UUID(card["version_id"])
            )
        )
        links = list(
            session.scalars(
                select(StyleCardSampleLink).where(
                    StyleCardSampleLink.style_card_version_id
                    == UUID(card["version_id"])
                )
            )
        )
        linked_samples = list(
            session.scalars(
                select(StyleSample).where(
                    StyleSample.id.in_([link.style_sample_id for link in links])
                )
            )
        )
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "style_card.main.generated"
            )
        )

    assert version is not None
    assert version.customer_id == CUSTOMER_A_ID
    assert len(links) == 10
    assert {sample.customer_id for sample in linked_samples} == {CUSTOMER_A_ID}
    assert {sample.photo_id for sample in linked_samples} == set(
        card["evidence_photo_ids"]
    )
    assert all(sample.withdrawn_at is None for sample in linked_samples)
    assert audit is not None
    assert audit.payload["style_card_version_id"] == card["version_id"]
    assert audit.payload["analyzer"] == "deterministic-local-v1"


def test_twenty_active_samples_produce_stable_generation(client: TestClient) -> None:
    authorize_samples(client, CUSTOMER_A, 20, prefix="stable-synthetic")

    response = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)

    assert response.status_code == 201
    assert response.json()["stability"] == "stable"
    assert response.json()["sample_count"] == 20


def test_edit_creates_new_immutable_version_and_preserves_evidence(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    authorize_samples(client, CUSTOMER_A, 10, prefix="editable-synthetic")
    generated_response = client.post(
        "/v1/style-cards/main/generate", headers=CUSTOMER_A
    )
    assert generated_response.status_code == 201
    generated = generated_response.json()

    response = client.patch(
        "/v1/style-cards/main",
        headers=CUSTOMER_A,
        json={"tone": ["自然", "克制"], "forbidden_elements": ["强烈滤镜"]},
    )

    assert response.status_code == 200
    edited = response.json()
    assert edited["version"] == generated["version"] + 1
    assert edited["version_id"] != generated["version_id"]
    assert edited["source"] == "customer_edit"
    assert edited["tone"] == ["自然", "克制"]
    assert edited["forbidden_elements"] == ["强烈滤镜"]
    assert edited["palette"] == generated["palette"]
    assert edited["evidence_photo_ids"] == generated["evidence_photo_ids"]

    with api_session_factory() as session:
        versions = list(
            session.scalars(select(StyleCardVersion).order_by(StyleCardVersion.version))
        )
        links = list(
            session.scalars(
                select(StyleCardSampleLink).where(
                    StyleCardSampleLink.style_card_version_id
                    == UUID(edited["version_id"])
                )
            )
        )
        audits = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type.like("style_card.main.%"))
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )

    assert [version.version for version in versions] == [1, 2]
    assert versions[0].content["tone"] == generated["tone"]
    assert versions[0].content["source"] == "analyzer"
    assert versions[1].content["tone"] == ["自然", "克制"]
    assert versions[1].content["source"] == "customer_edit"
    assert len(links) == 10
    assert [event.event_type for event in audits] == [
        "style_card.main.generated",
        "style_card.main.edited",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"tone": ["   "]},
        {"tone": ["x" * 201]},
        {"tone": [f"item-{number}" for number in range(21)]},
    ],
)
def test_edit_rejects_invalid_or_oversized_style_entries(
    client: TestClient, payload: dict[str, list[str]]
) -> None:
    authorize_samples(client, CUSTOMER_A, 10, prefix="bounded-edit")
    generated = client.post(
        "/v1/style-cards/main/generate", headers=CUSTOMER_A
    )
    assert generated.status_code == 201

    response = client.patch(
        "/v1/style-cards/main", headers=CUSTOMER_A, json=payload
    )

    assert response.status_code == 422


def test_edit_strips_entries_and_allows_intentional_empty_list_clear(
    client: TestClient,
) -> None:
    authorize_samples(client, CUSTOMER_A, 10, prefix="clear-edit")
    generated = client.post(
        "/v1/style-cards/main/generate", headers=CUSTOMER_A
    )
    assert generated.status_code == 201

    normalized = client.patch(
        "/v1/style-cards/main",
        headers=CUSTOMER_A,
        json={"palette": ["  暖白  "]},
    )
    cleared = client.patch(
        "/v1/style-cards/main",
        headers=CUSTOMER_A,
        json={"forbidden_elements": []},
    )

    assert normalized.status_code == 200
    assert normalized.json()["palette"] == ["暖白"]
    assert cleared.status_code == 200
    assert cleared.json()["forbidden_elements"] == []


def test_edit_rejects_no_actual_content_change_without_new_version(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    authorize_samples(client, CUSTOMER_A, 10, prefix="no-change")
    generated = client.post(
        "/v1/style-cards/main/generate", headers=CUSTOMER_A
    ).json()

    same_value = client.patch(
        "/v1/style-cards/main",
        headers=CUSTOMER_A,
        json={"tone": generated["tone"]},
    )

    assert same_value.status_code == 409
    assert same_value.json() == {"error": {"code": "style_card_no_changes"}}
    with api_session_factory() as session:
        assert session.query(StyleCardVersion).count() == 1
        assert session.query(AuditEvent).filter(
            AuditEvent.event_type == "style_card.main.edited"
        ).count() == 0


def test_regeneration_and_get_return_latest_version_for_only_request_customer(
    client: TestClient,
) -> None:
    authorize_samples(client, CUSTOMER_A, 10, prefix="customer-a-synthetic")
    authorize_samples(client, CUSTOMER_B, 10, prefix="customer-b-synthetic")
    first = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A).json()
    other = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_B).json()

    edited = client.patch(
        "/v1/style-cards/main", headers=CUSTOMER_A, json={"lighting": ["柔光"]}
    ).json()
    regenerated_response = client.post(
        "/v1/style-cards/main/generate", headers=CUSTOMER_A
    )

    assert regenerated_response.status_code == 201
    regenerated = regenerated_response.json()
    assert regenerated["id"] == first["id"]
    assert regenerated["version"] == 3
    assert regenerated["source"] == "analyzer"
    assert regenerated["version_id"] not in {
        first["version_id"],
        edited["version_id"],
    }
    assert regenerated["id"] != other["id"]

    latest = client.get("/v1/style-cards/main", headers=CUSTOMER_A)
    assert latest.status_code == 200
    assert latest.json() == regenerated


def test_get_and_edit_hide_missing_main_card(client: TestClient) -> None:
    get_response = client.get("/v1/style-cards/main", headers=CUSTOMER_A)
    edit_response = client.patch(
        "/v1/style-cards/main", headers=CUSTOMER_A, json={"tone": ["自然"]}
    )

    assert get_response.status_code == 404
    assert get_response.json() == {"error": {"code": "main_style_card_not_found"}}
    assert edit_response.status_code == 404
    assert edit_response.json() == {"error": {"code": "main_style_card_not_found"}}


@pytest.mark.parametrize("payload", [{}, {"tone": None}])
def test_edit_rejects_empty_payload(
    client: TestClient, payload: dict[str, object]
) -> None:
    authorize_samples(client, CUSTOMER_A, 10, prefix="editable-synthetic")
    client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)

    response = client.patch("/v1/style-cards/main", headers=CUSTOMER_A, json=payload)

    assert response.status_code == 422


@pytest.fixture
def generated_style_card(client: TestClient) -> dict[str, Any]:
    authorize_samples(client, CUSTOMER_A, 10, prefix="scene-main")
    response = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def five_active_scenes(
    client: TestClient, generated_style_card: dict[str, Any]
) -> list[Any]:
    del generated_style_card
    scenes = []
    for number in range(5):
        response = client.post(
            "/v1/style-cards/scenes",
            headers=CUSTOMER_A,
            json={"name": f"场景 {number + 1}"},
        )
        scenes.append(response)
    return scenes


def test_scene_create_preserves_channel_fields_and_inherits_main_tone(
    client: TestClient, generated_style_card: dict[str, Any]
) -> None:
    response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={
            "name": "商城商品图",
            "backgrounds": ["暖白纯色"],
            "aspect_ratio": "4:5",
            "purpose": "商城商品详情主图",
            "usage_rules": ["保留完整商品主体", "不得冒充实物图"],
        },
    )

    assert response.status_code == 201
    scene = response.json()
    assert scene["tone"] == generated_style_card["tone"]
    assert scene["backgrounds"] == ["暖白纯色"]
    assert scene["aspect_ratio"] == "4:5"
    assert scene["purpose"] == "商城商品详情主图"
    assert scene["usage_rules"] == ["保留完整商品主体", "不得冒充实物图"]
    assert scene["overrides"] == {"backgrounds": ["暖白纯色"]}


def test_sixth_active_scene_is_rejected(
    client: TestClient, five_active_scenes: list[Any]
) -> None:
    assert [response.status_code for response in five_active_scenes] == [201] * 5

    response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "第六场景"},
    )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "scene_limit_reached"}}


def test_scene_edit_creates_new_version_from_same_pinned_main(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    created_response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={
            "name": "品牌海报",
            "backgrounds": ["暖灰背景"],
            "aspect_ratio": "4:5",
            "purpose": "品牌展示",
            "usage_rules": ["保留品牌留白"],
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()
    changed_main = client.patch(
        "/v1/style-cards/main",
        headers=CUSTOMER_A,
        json={"tone": ["后来的主卡色调"]},
    )
    assert changed_main.status_code == 200

    edited_response = client.patch(
        f"/v1/style-cards/scenes/{created['id']}",
        headers=CUSTOMER_A,
        json={
            "backgrounds": ["深灰背景"],
            "aspect_ratio": "9:16",
            "purpose": "竖版品牌故事",
            "usage_rules": ["仅用于品牌海报", "保留商品真实纹理"],
        },
    )

    assert edited_response.status_code == 200
    edited = edited_response.json()
    assert edited["version"] == 2
    assert edited["version_id"] != created["version_id"]
    assert edited["main_style_card_version_id"] == generated_style_card["version_id"]
    assert edited["tone"] == generated_style_card["tone"]
    assert edited["backgrounds"] == ["深灰背景"]
    assert edited["aspect_ratio"] == "9:16"
    assert edited["purpose"] == "竖版品牌故事"
    assert edited["usage_rules"] == ["仅用于品牌海报", "保留商品真实纹理"]
    assert edited["overrides"] == {"backgrounds": ["深灰背景"]}

    with api_session_factory() as session:
        versions = list(
            session.scalars(
                select(StyleCardVersion)
                .where(StyleCardVersion.style_card_id == UUID(created["id"]))
                .order_by(StyleCardVersion.version)
            )
        )

    assert [version.version for version in versions] == [1, 2]
    assert versions[0].content["overrides"] == {"backgrounds": ["暖灰背景"]}
    assert versions[1].content["overrides"] == {"backgrounds": ["深灰背景"]}
    assert versions[0].content["aspect_ratio"] == "4:5"
    assert versions[0].content["purpose"] == "品牌展示"
    assert versions[0].content["usage_rules"] == ["保留品牌留白"]
    assert versions[1].content["aspect_ratio"] == "9:16"
    assert versions[1].content["purpose"] == "竖版品牌故事"
    assert versions[1].content["usage_rules"] == [
        "仅用于品牌海报",
        "保留商品真实纹理",
    ]
    assert "tone" not in versions[0].content
    assert "tone" not in versions[1].content


def test_scene_edit_can_explicitly_clear_fields_and_restore_inheritance(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    created_response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={
            "name": "可清空场景",
            "backgrounds": ["场景专属背景"],
            "purpose": "临时用途",
            "usage_rules": ["临时规则"],
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()

    response = client.patch(
        f"/v1/style-cards/scenes/{created['id']}",
        headers=CUSTOMER_A,
        json={"backgrounds": None, "purpose": None, "usage_rules": []},
    )

    assert response.status_code == 200
    edited = response.json()
    assert edited["version"] == 2
    assert edited["backgrounds"] == generated_style_card["backgrounds"]
    assert edited["purpose"] is None
    assert edited["usage_rules"] == []
    assert edited["overrides"] == {}
    with api_session_factory() as session:
        versions = list(
            session.scalars(
                select(StyleCardVersion)
                .where(StyleCardVersion.style_card_id == UUID(created["id"]))
                .order_by(StyleCardVersion.version)
            )
        )
    assert versions[0].content["purpose"] == "临时用途"
    assert versions[0].content["usage_rules"] == ["临时规则"]
    assert "purpose" not in versions[1].content
    assert versions[1].content["usage_rules"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"aspect_ratio": "7:13"},
        {"purpose": " "},
        {"purpose": "x" * 201},
        {"usage_rules": "not-a-list"},
        {"usage_rules": [" "]},
        {"usage_rules": ["x" * 201]},
        {"usage_rules": [f"rule-{number}" for number in range(21)]},
    ],
)
def test_scene_create_and_edit_reject_invalid_channel_fields(
    client: TestClient,
    generated_style_card: dict[str, Any],
    payload: dict[str, object],
) -> None:
    del generated_style_card
    create_response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "非法创建场景", **payload},
    )
    valid_scene = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "合法编辑场景"},
    ).json()
    edit_response = client.patch(
        f"/v1/style-cards/scenes/{valid_scene['id']}",
        headers=CUSTOMER_A,
        json=payload,
    )

    assert create_response.status_code == 422
    assert edit_response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"aspect_ratio": "4:5"},
        {"purpose": "商城主图"},
        {"usage_rules": ["仅用于商城"]},
    ],
)
def test_main_card_edit_rejects_scene_only_fields(
    client: TestClient,
    generated_style_card: dict[str, Any],
    payload: dict[str, object],
) -> None:
    del generated_style_card

    response = client.patch("/v1/style-cards/main", headers=CUSTOMER_A, json=payload)

    assert response.status_code == 422


def test_scene_deactivation_frees_an_active_slot_without_deleting_history(
    client: TestClient,
    five_active_scenes: list[Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    assert [response.status_code for response in five_active_scenes] == [201] * 5
    first_scene = five_active_scenes[0].json()

    deactivated_response = client.post(
        f"/v1/style-cards/scenes/{first_scene['id']}/deactivate",
        headers=CUSTOMER_A,
    )
    replacement_response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "替补场景"},
    )

    assert deactivated_response.status_code == 200
    assert deactivated_response.json()["status"] == "inactive"
    assert replacement_response.status_code == 201
    with api_session_factory() as session:
        preserved_versions = list(
            session.scalars(
                select(StyleCardVersion).where(
                    StyleCardVersion.style_card_id == UUID(first_scene["id"])
                )
            )
        )
        deactivation_audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.customer_id == CUSTOMER_A_ID,
                AuditEvent.event_type == "style_card.scene.deactivated",
            )
        )

    assert len(preserved_versions) == 1
    assert deactivation_audit is not None


def test_repeated_scene_deactivation_returns_same_snapshot_without_new_history(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    del generated_style_card
    scene = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "幂等停用场景", "backgrounds": ["暖白"]},
    ).json()

    first = client.post(
        f"/v1/style-cards/scenes/{scene['id']}/deactivate",
        headers=CUSTOMER_A,
    )
    repeated = client.post(
        f"/v1/style-cards/scenes/{scene['id']}/deactivate",
        headers=CUSTOMER_A,
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    with api_session_factory() as session:
        assert session.query(StyleCardVersion).filter(
            StyleCardVersion.style_card_id == UUID(scene["id"])
        ).count() == 1
        assert session.query(AuditEvent).filter(
            AuditEvent.customer_id == CUSTOMER_A_ID,
            AuditEvent.event_type == "style_card.scene.deactivated",
        ).count() == 1


def test_deactivation_retry_after_lost_success_response_remains_idempotent(
    client: TestClient,
    five_active_scenes: list[Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    assert [response.status_code for response in five_active_scenes] == [201] * 5
    scene = five_active_scenes[0].json()

    client.post(
        f"/v1/style-cards/scenes/{scene['id']}/deactivate",
        headers=CUSTOMER_A,
    )
    retry = client.post(
        f"/v1/style-cards/scenes/{scene['id']}/deactivate",
        headers=CUSTOMER_A,
    )
    replacement = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "丢失响应后的替补场景"},
    )

    assert retry.status_code == 200
    assert retry.json()["status"] == "inactive"
    assert replacement.status_code == 201
    with api_session_factory() as session:
        assert session.query(AuditEvent).filter(
            AuditEvent.customer_id == CUSTOMER_A_ID,
            AuditEvent.event_type == "style_card.scene.deactivated",
        ).count() == 1


def test_scene_list_includes_inactive_history_for_only_request_customer(
    client: TestClient, generated_style_card: dict[str, Any]
) -> None:
    del generated_style_card
    first = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "商城图"},
    ).json()
    second = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "朋友圈图"},
    ).json()
    deactivated = client.post(
        f"/v1/style-cards/scenes/{first['id']}/deactivate",
        headers=CUSTOMER_A,
    )
    assert deactivated.status_code == 200

    authorize_samples(client, CUSTOMER_B, 10, prefix="scene-customer-b")
    other_main = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_B)
    assert other_main.status_code == 201
    other_scene = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_B,
        json={"name": "其他客户场景"},
    )
    assert other_scene.status_code == 201

    response = client.get("/v1/style-cards/scenes", headers=CUSTOMER_A)

    assert response.status_code == 200
    scenes = response.json()
    assert [scene["id"] for scene in scenes] == [first["id"], second["id"]]
    assert [scene["status"] for scene in scenes] == ["inactive", "active"]
    assert other_scene.json()["id"] not in response.text


def test_scene_creation_requires_an_active_main_card(client: TestClient) -> None:
    response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "没有主卡的场景"},
    )

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "main_style_card_not_found"}}


def test_duplicate_active_scene_name_on_create_is_non_retryable_conflict(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    del generated_style_card
    first = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "重名场景"},
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "重名场景"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"error": {"code": "scene_name_conflict"}}
    with api_session_factory() as session:
        assert session.query(SceneStyle).filter(
            SceneStyle.customer_id == CUSTOMER_A_ID,
            SceneStyle.name == "重名场景",
            SceneStyle.active.is_(True),
        ).count() == 1
        assert session.query(AuditEvent).filter(
            AuditEvent.customer_id == CUSTOMER_A_ID,
            AuditEvent.event_type == "style_card.scene.created",
        ).count() == 1


def test_duplicate_active_scene_name_on_rename_is_non_retryable_conflict(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    del generated_style_card
    first = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "待改名场景"},
    ).json()
    existing = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "已存在场景"},
    ).json()

    duplicate = client.patch(
        f"/v1/style-cards/scenes/{first['id']}",
        headers=CUSTOMER_A,
        json={"name": existing["name"]},
    )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"error": {"code": "scene_name_conflict"}}
    with api_session_factory() as session:
        first_scene = session.get(SceneStyle, UUID(first["id"]))
        assert first_scene is not None and first_scene.name == "待改名场景"
        assert session.query(StyleCardVersion).filter(
            StyleCardVersion.style_card_id == UUID(first["id"])
        ).count() == 1
        assert session.query(AuditEvent).filter(
            AuditEvent.customer_id == CUSTOMER_A_ID,
            AuditEvent.event_type == "style_card.scene.edited",
        ).count() == 0


def test_scene_mutations_hide_another_customers_card(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    del generated_style_card
    scene = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "A 客户场景"},
    ).json()

    edit_response = client.patch(
        f"/v1/style-cards/scenes/{scene['id']}",
        headers=CUSTOMER_B,
        json={"backgrounds": ["不应写入"]},
    )
    deactivate_response = client.post(
        f"/v1/style-cards/scenes/{scene['id']}/deactivate",
        headers=CUSTOMER_B,
    )

    assert edit_response.status_code == 404
    assert edit_response.json() == {"error": {"code": "scene_style_card_not_found"}}
    assert deactivate_response.status_code == 404
    assert deactivate_response.json() == {
        "error": {"code": "scene_style_card_not_found"}
    }
    with api_session_factory() as session:
        card = session.get(StyleCard, UUID(scene["id"]))
        versions = list(
            session.scalars(
                select(StyleCardVersion).where(
                    StyleCardVersion.style_card_id == UUID(scene["id"])
                )
            )
        )
    assert card is not None and card.active is True
    assert len(versions) == 1


def test_scene_edit_rejects_no_actual_change_without_new_version(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
) -> None:
    del generated_style_card
    scene = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "无变化场景", "backgrounds": ["暖白"]},
    ).json()

    response = client.patch(
        f"/v1/style-cards/scenes/{scene['id']}",
        headers=CUSTOMER_A,
        json={"backgrounds": ["暖白"]},
    )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "style_card_no_changes"}}
    with api_session_factory() as session:
        versions = list(
            session.scalars(
                select(StyleCardVersion).where(
                    StyleCardVersion.style_card_id == UUID(scene["id"])
                )
            )
        )
    assert len(versions) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("aspect_ratio", "4:5"),
        ("purpose", "商城详情图"),
        ("usage_rules", ["保留商品主体"]),
    ],
)
def test_scene_channel_field_no_op_does_not_append_version(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
    field: str,
    value: str | list[str],
) -> None:
    del generated_style_card
    create_response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": f"{field} 无变化", field: value},
    )
    assert create_response.status_code == 201
    scene = create_response.json()

    response = client.patch(
        f"/v1/style-cards/scenes/{scene['id']}",
        headers=CUSTOMER_A,
        json={field: value},
    )

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "style_card_no_changes"}}
    with api_session_factory() as session:
        assert session.query(StyleCardVersion).filter(
            StyleCardVersion.style_card_id == UUID(scene["id"])
        ).count() == 1


def test_held_sqlite_write_lock_makes_scene_creation_retryable_without_writes(
    lock_contention_client: TestClient,
    api_session_factory: sessionmaker[Session],
) -> None:
    authorize_samples(
        lock_contention_client,
        CUSTOMER_A,
        10,
        prefix="locked-scene",
    )
    generated = lock_contention_client.post(
        "/v1/style-cards/main/generate", headers=CUSTOMER_A
    )
    assert generated.status_code == 201

    with api_session_factory() as locker:
        locker.connection().exec_driver_sql("BEGIN IMMEDIATE")
        response = lock_contention_client.post(
            "/v1/style-cards/scenes",
            headers=CUSTOMER_A,
            json={"name": "锁竞争场景"},
        )
        locker.rollback()

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "style_card_write_conflict", "retryable": True}
    }
    with api_session_factory() as session:
        assert session.query(SceneStyle).count() == 0
        assert session.query(StyleCard).filter(StyleCard.kind == "scene").count() == 0
        assert session.query(StyleCardVersion).count() == 1
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "style_card.scene.created")
            .count()
            == 0
        )


def test_residual_scene_version_conflict_remains_retryable(
    client: TestClient,
    generated_style_card: dict[str, Any],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del generated_style_card
    monkeypatch.setattr(
        routes_styles_module,
        "SceneStyleCardService",
        ResidualSceneVersionConflictService,
    )

    response = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "版本竞态场景"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "style_card_write_conflict", "retryable": True}
    }
    with api_session_factory() as session:
        assert session.query(SceneStyle).count() == 0
        assert session.query(StyleCard).filter(StyleCard.kind == "scene").count() == 0
        assert session.query(StyleCardVersion).count() == 1
        assert session.query(AuditEvent).filter(
            AuditEvent.event_type == "style_card.scene.created"
        ).count() == 0
