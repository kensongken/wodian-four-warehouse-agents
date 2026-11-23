"""Deterministic local authenticity gate contracts."""

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import MethodType
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.api.routes_authenticity import get_authenticity_checker
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.domain.authenticity import (
    REQUIRED_AUTHENTICITY_CHECKS as REQUIRED_CHECK_ORDER,
)
from opc_style.domain.authenticity import (
    AuthenticityResult,
    AuthenticityRuleResult,
    DeterministicLocalAuthenticityChecker,
)
from opc_style.domain.image_generator import (
    DeterministicLocalImageGenerator,
    GenerationRequest,
)
from opc_style.domain.image_normalizer import NormalizedPayload
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    AuthenticityCheck,
    AuthenticityCompletion,
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
from opc_style.services.authenticity import AuthenticityService
from opc_style.services.image_jobs import _forget_local_store, _remember_local_store
from opc_style.services.real_photos import (
    LocalSyntheticAssetStore,
    RealPhotoOutputService,
)

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}
REQUIRED_CHECKS = {
    "subject_match",
    "component_count",
    "color_shift",
    "artifact_scan",
    "dimensions",
}
REAL_PHOTO_STYLE_SCENE_METADATA_KEYS = (
    "style_card_version_id",
    "main_style_card_version_id",
    "scene_style_id",
    "scene_style_card_id",
    "style_card_id",
    "previous_style_card_version_id",
    "scene_id",
    "scene_card_id",
)


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'authenticity.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def content_store(
    tmp_path: Path,
) -> Generator[LocalSyntheticAssetStore, None, None]:
    with LocalSyntheticAssetStore(tmp_path / "authenticity-assets") as store:
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
def job_factory(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
) -> Generator[Callable[..., UUID], None, None]:
    remembered: list[UUID] = []

    def create_job(
        *,
        customer_id: UUID = CUSTOMER_A_ID,
        status: str = "machine_check",
        candidate_count: int = 3,
        failing_sequence: int | None = None,
        corrupt_sequence: int | None = None,
        source_trace_mismatch: bool = False,
        style_case: str = "valid",
        adapter_version: str = "deterministic-local-image-v1",
        candidate_trace_mismatch: bool = False,
    ) -> UUID:
        job_id = uuid4()
        asset_id = uuid4()
        product_id = f"product-{job_id}"
        expected_sequences = (1, 2, 3)
        capability = content_store.issue_cleanup_capability(
            customer_id, job_id, expected_sequences
        )
        with api_session_factory() as session:
            if style_case in {"cross_customer", "random"}:
                session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
            if session.get(Customer, customer_id) is None:
                session.add(Customer(id=customer_id, display_name="Synthetic customer"))
            style_owner_id = (
                CUSTOMER_B_ID if style_case == "cross_customer" else customer_id
            )
            if session.get(Customer, style_owner_id) is None:
                session.add(Customer(id=style_owner_id, display_name="Style owner"))
            main_card = (
                session.query(StyleCard)
                .filter_by(customer_id=style_owner_id, kind="main", active=True)
                .first()
            )
            if main_card is None:
                main_card = StyleCard(
                    customer_id=style_owner_id,
                    kind="main",
                    active=style_case != "inactive",
                )
                session.add(main_card)
                session.flush()
                main_version = StyleCardVersion(
                    customer_id=style_owner_id,
                    style_card_id=main_card.id,
                    version=1,
                    content={"tone": ["synthetic"]},
                )
                session.add(main_version)
                session.flush()
            else:
                main_version = (
                    session.query(StyleCardVersion)
                    .filter_by(
                        customer_id=style_owner_id,
                        style_card_id=main_card.id,
                    )
                    .one()
                )
            scene_id = uuid4()
            scene_card = StyleCard(
                id=scene_id,
                customer_id=style_owner_id,
                kind="scene",
                active=style_case != "inactive",
            )
            session.add(scene_card)
            session.flush()
            scene = SceneStyle(
                id=scene_id,
                customer_id=style_owner_id,
                style_card_id=scene_card.id,
                name=f"Synthetic scene {scene_id}",
                active=style_case != "inactive",
                rules={},
            )
            scene_version = StyleCardVersion(
                customer_id=style_owner_id,
                style_card_id=scene_card.id,
                version=1,
                content={
                    "main_style_card_version_id": str(main_version.id),
                    "source": "synthetic_test",
                },
            )
            session.add_all([scene, scene_version])
            session.flush()
            if style_case == "random":
                pinned_scene_id = uuid4()
                pinned_scene_version_id = uuid4()
            elif style_case == "null":
                pinned_scene_id = None
                pinned_scene_version_id = None
            else:
                pinned_scene_id = scene.id
                pinned_scene_version_id = scene_version.id
            request_scene_id = (
                uuid4() if style_case == "metadata_mismatch" else scene.id
            )
            session.add(
                ProductAsset(
                    id=asset_id,
                    customer_id=customer_id,
                    product_id=(
                        "different-product-fact"
                        if source_trace_mismatch
                        else product_id
                    ),
                    photo_id=f"photo-{job_id}",
                    identity="original_real_photo",
                    confirmed=True,
                    source_type="synthetic_metadata",
                    quality_flags=["synthetic_fixture"],
                    occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
                    content_sha256="a" * 64,
                    mime_type="image/jpeg",
                    width=512,
                    height=512,
                    storage_path=None,
                )
            )
            job = ImageOutputJob(
                id=job_id,
                customer_id=customer_id,
                output_type="scene",
                output_ratio="1:1",
                style_card_version_id=pinned_scene_version_id,
                scene_style_id=pinned_scene_id,
                adapter_version=adapter_version,
                artifact_store_identity=content_store.store_identity,
                artifact_store_namespace=content_store.store_namespace,
                artifact_store_capability=capability,
                expected_artifact_sequences=list(expected_sequences),
                status=status,
                request_metadata={
                    "sources": [{"product_id": product_id, "asset_id": str(asset_id)}],
                    "source_product_ids": [product_id],
                    "source_asset_ids": [str(asset_id)],
                    "style_card_version_id": str(scene_version.id),
                    "main_style_card_version_id": str(main_version.id),
                    "scene_style_id": str(request_scene_id),
                    "scene_style_card_id": str(scene_card.id),
                    "generation_mode": "synthetic_test_only",
                    "synthetic_test_only": True,
                    "publishable": False,
                },
                idempotency_key=f"authenticity-{job_id}",
            )
            session.add(job)
            session.flush()
            session.add(
                ProductAssetLink(
                    customer_id=customer_id,
                    product_asset_id=asset_id,
                    output_job_id=job_id,
                    position=1,
                )
            )
            for sequence_number in range(1, candidate_count + 1):
                if sequence_number == corrupt_sequence:
                    content = b"not a jpeg, but hash and store registration are valid"
                else:
                    output = BytesIO()
                    Image.new("RGB", (512, 512), (20, 40, 60)).save(
                        output, format="JPEG"
                    )
                    content = output.getvalue()
                fixture: dict[str, object] = {
                    "subject_complete": True,
                    "product_shape_preserved": True,
                    "material_preserved": True,
                    "component_count_preserved": True,
                    "color_shift_within_fixture_tolerance": True,
                    "unexpected_artifacts": [],
                }
                if sequence_number == failing_sequence:
                    fixture["material_preserved"] = False
                metadata: dict[str, object] = {
                    "output_job_id": str(job_id),
                    "adapter_version": job.adapter_version,
                    "generation_mode": "synthetic_test_only",
                    "synthetic_test_only": True,
                    "publishable": False,
                    "sequence_number": sequence_number,
                    "output_type": "scene",
                    "output_ratio": "1:1",
                    "source_product_ids": [product_id],
                    "source_asset_ids": [str(asset_id)],
                    "style_card_version_id": job.request_metadata[
                        "style_card_version_id"
                    ],
                    "main_style_card_version_id": job.request_metadata[
                        "main_style_card_version_id"
                    ],
                    "scene_style_id": job.request_metadata["scene_style_id"],
                    "scene_style_card_id": job.request_metadata["scene_style_card_id"],
                    "local_authenticity_fixture": fixture,
                }
                if candidate_trace_mismatch and sequence_number == 1:
                    metadata["style_card_version_id"] = str(uuid4())
                    metadata["output_type"] = "real_photo"
                digest = sha256(content).hexdigest()
                target = content_store.reserve_derivative(
                    customer_id, job_id, sequence_number
                )
                result = content_store.create_derivative(
                    target,
                    NormalizedPayload(
                        content=content,
                        content_sha256=digest,
                        mime_type="image/jpeg",
                        width=512,
                        height=512,
                        metadata=metadata,
                    ),
                )
                session.add(
                    ImageCandidate(
                        customer_id=customer_id,
                        output_job_id=job_id,
                        sequence_number=sequence_number,
                        identity=(
                            "normalized_real_photo_derivative"
                            if candidate_trace_mismatch and sequence_number == 1
                            else "synthetic_style_image_candidate"
                        ),
                        artifact_path=str(result.output_path),
                        content_sha256=digest,
                        mime_type="image/jpeg",
                        width=512,
                        height=512,
                        adapter_version=job.adapter_version,
                        artifact_metadata=metadata,
                    )
                )
            session.commit()
            if style_case in {"cross_customer", "random"}:
                session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        _remember_local_store(job_id, content_store)
        remembered.append(job_id)
        return job_id

    yield create_job
    for job_id in remembered:
        _forget_local_store(job_id, content_store)


def test_all_three_candidates_must_pass_before_customer_review(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
) -> None:
    job_id = job_factory()

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == str(job_id)
    assert body["status"] == "awaiting_customer_review"
    assert body["checker_version"] == "deterministic-local-authenticity-rules-v1"
    assert body["check_mode"] == "synthetic_local_rule_based"
    assert body["visual_ai"] is False
    assert body["customer_approved"] is False
    assert body["publishable"] is False
    assert len(body["candidates"]) == 3
    assert {item["candidate_state"] for item in body["candidates"]} == {
        "awaiting_customer_review"
    }
    for candidate in body["candidates"]:
        assert {check["check_type"] for check in candidate["checks"]} == REQUIRED_CHECKS
        assert all(check["passed"] for check in candidate["checks"])
        assert all(
            check["details"]["visual_ai"] is False for check in candidate["checks"]
        )

    status_response = client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_A)
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "awaiting_customer_review"
    assert {
        item["candidate_state"] for item in status_response.json()["candidates"]
    } == {"awaiting_customer_review"}
    with api_session_factory() as session:
        job = session.get(ImageOutputJob, job_id)
        assert job is not None and job.status == "awaiting_customer_review"
        assert session.query(AuthenticityCheck).count() == 15
        completion = session.query(AuthenticityCompletion).one()
        assert completion.output_job_id == job_id
        assert completion.result_status == "awaiting_customer_review"
        assert len(completion.candidate_batch_digest) == 64
        assert (
            session.query(AuditEvent)
            .filter_by(event_type="image_output.authenticity_completed")
            .count()
            == 1
        )
        event = (
            session.query(AuditEvent)
            .filter_by(event_type="image_output.authenticity_completed")
            .one()
        )
        assert event.payload["completion_id"] == str(completion.id)
        assert event.payload["candidate_batch_digest"] == (
            completion.candidate_batch_digest
        )


def test_one_failed_candidate_blocks_the_whole_job_without_rewriting_candidates(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
) -> None:
    job_id = job_factory(failing_sequence=2)
    with api_session_factory() as session:
        original_facts = [
            (
                item.id,
                item.content_sha256,
                item.artifact_metadata,
                item.created_at,
            )
            for item in session.query(ImageCandidate)
            .filter_by(output_job_id=job_id)
            .order_by(ImageCandidate.sequence_number)
        ]

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "authenticity_failed"
    states = {
        item["sequence_number"]: item["candidate_state"]
        for item in response.json()["candidates"]
    }
    assert states == {
        1: "authenticity_failed",
        2: "authenticity_failed",
        3: "authenticity_failed",
    }
    with api_session_factory() as session:
        job = session.get(ImageOutputJob, job_id)
        assert job is not None and job.status == "authenticity_failed"
        assert job.failure_reason == "authenticity_check_failed"
        assert session.query(AuthenticityCheck).count() == 15
        current_facts = [
            (
                item.id,
                item.content_sha256,
                item.artifact_metadata,
                item.created_at,
            )
            for item in session.query(ImageCandidate)
            .filter_by(output_job_id=job_id)
            .order_by(ImageCandidate.sequence_number)
        ]
        assert current_facts == original_facts


def test_gate_rejects_wrong_state_wrong_count_and_partial_history_without_progress(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
) -> None:
    queued_job = job_factory(status="queued")
    short_job = job_factory(candidate_count=2)
    partial_job = job_factory()
    with api_session_factory() as session:
        candidate = (
            session.query(ImageCandidate)
            .filter_by(output_job_id=partial_job, sequence_number=1)
            .one()
        )
        session.add(
            AuthenticityCheck(
                customer_id=CUSTOMER_A_ID,
                image_candidate_id=candidate.id,
                check_type="subject_match",
                passed=True,
                reason="legacy_partial_result",
                checker_version="deterministic-local-authenticity-rules-v1",
                details={"visual_ai": False},
            )
        )
        session.commit()

    queued = client.post(
        f"/v1/image-outputs/{queued_job}/authenticity-checks", headers=CUSTOMER_A
    )
    short = client.post(
        f"/v1/image-outputs/{short_job}/authenticity-checks", headers=CUSTOMER_A
    )
    partial = client.post(
        f"/v1/image-outputs/{partial_job}/authenticity-checks", headers=CUSTOMER_A
    )

    assert queued.status_code == 409
    assert queued.json()["error"]["code"] == "authenticity_job_not_machine_check"
    assert short.status_code == 409
    assert (
        short.json()["error"]["code"]
        == "authenticity_requires_exactly_three_candidates"
    )
    assert partial.status_code == 409
    assert partial.json()["error"]["code"] == "authenticity_check_history_conflict"
    partial_status = client.get(f"/v1/image-outputs/{partial_job}", headers=CUSTOMER_A)
    assert partial_status.status_code == 200
    assert partial_status.json()["status"] == "machine_check"
    assert partial_status.json()["candidates"][0]["candidate_state"] == (
        "pending_authenticity_check"
    )
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, queued_job).status == "queued"
        assert session.get(ImageOutputJob, short_job).status == "machine_check"
        assert session.get(ImageOutputJob, partial_job).status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 1


def test_corrupt_jpeg_fails_artifact_scan_and_never_approves_or_publishes(
    client: TestClient,
    job_factory: Callable[..., UUID],
) -> None:
    job_id = job_factory(corrupt_sequence=3)

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "authenticity_failed"
    third = response.json()["candidates"][2]
    artifact_scan = next(
        check for check in third["checks"] if check["check_type"] == "artifact_scan"
    )
    assert artifact_scan["passed"] is False
    assert artifact_scan["reason"] == "artifact_not_decodable_jpeg"
    assert response.json()["customer_approved"] is False
    assert response.json()["publishable"] is False


def test_database_source_product_mismatch_fails_subject_trace_rule(
    client: TestClient,
    job_factory: Callable[..., UUID],
) -> None:
    job_id = job_factory(source_trace_mismatch=True)

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 201
    assert response.json()["status"] == "authenticity_failed"
    for candidate in response.json()["candidates"]:
        subject = next(
            check
            for check in candidate["checks"]
            if check["check_type"] == "subject_match"
        )
        assert subject["passed"] is False
        assert subject["reason"] == "database_source_provenance_mismatch"


def test_retry_is_idempotent_and_concurrent_calls_append_one_result_set(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
) -> None:
    job_id = job_factory()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _: client.post(
                    f"/v1/image-outputs/{job_id}/authenticity-checks",
                    headers=CUSTOMER_A,
                ),
                range(2),
            )
        )
    replay = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert sorted(response.status_code for response in responses) == [200, 201]
    assert replay.status_code == 200
    assert responses[0].json() == responses[1].json() == replay.json()
    with api_session_factory() as session:
        assert session.query(AuthenticityCheck).count() == 15
        assert session.query(AuthenticityCompletion).count() == 1
        assert (
            session.query(AuditEvent)
            .filter_by(event_type="image_output.authenticity_completed")
            .count()
            == 1
        )


def test_completion_seal_rolls_back_with_checks_when_commit_fails(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = job_factory()

    def fail_commit(session: Session) -> None:
        del session
        raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 503, response.text
    with api_session_factory() as session:
        job = session.get(ImageOutputJob, job_id)
        assert job is not None and job.status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 0
        assert session.query(AuthenticityCompletion).count() == 0
        assert (
            session.query(AuditEvent)
            .filter_by(event_type="image_output.authenticity_completed")
            .count()
            == 0
        )


def test_completion_seal_ack_loss_replays_one_durable_batch(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = job_factory()
    original_commit = Session.commit
    lost = False

    def commit_then_lose_ack(session: Session) -> None:
        nonlocal lost
        original_commit(session)
        if not lost:
            lost = True
            raise RuntimeError("synthetic completion acknowledgement loss")

    monkeypatch.setattr(Session, "commit", commit_then_lose_ack)
    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_customer_review"
    with api_session_factory() as session:
        assert session.query(AuthenticityCheck).count() == 15
        assert session.query(AuthenticityCompletion).count() == 1
        assert (
            session.query(AuditEvent)
            .filter_by(event_type="image_output.authenticity_completed")
            .count()
            == 1
        )


def test_foreign_job_is_only_a_404_and_restart_loses_machine_check_capability(
    client: TestClient,
    job_factory: Callable[..., UUID],
    content_store: LocalSyntheticAssetStore,
) -> None:
    foreign_job = job_factory()
    restarted_job = job_factory()
    _forget_local_store(restarted_job, content_store)

    foreign = client.post(
        f"/v1/image-outputs/{foreign_job}/authenticity-checks", headers=CUSTOMER_B
    )
    restarted = client.post(
        f"/v1/image-outputs/{restarted_job}/authenticity-checks", headers=CUSTOMER_A
    )

    assert foreign.status_code == 404
    assert restarted.status_code == 503
    assert restarted.json() == {
        "error": {"code": "authenticity_artifact_outcome_uncertain", "retryable": True}
    }


def test_deterministic_generator_marks_every_local_authenticity_rule_fixture() -> None:
    request = GenerationRequest(
        output_type="scene",
        product_ids=("product-001",),
        asset_ids=(uuid4(),),
        style_card_version_id=uuid4(),
        main_style_card_version_id=uuid4(),
        scene_style_id=uuid4(),
        output_ratio="1:1",
    )

    candidates = DeterministicLocalImageGenerator().generate(request)

    for candidate in candidates:
        assert candidate.metadata["local_authenticity_fixture"] == {
            "subject_complete": True,
            "product_shape_preserved": True,
            "material_preserved": True,
            "component_count_preserved": True,
            "color_shift_within_fixture_tolerance": True,
            "unexpected_artifacts": [],
        }


class ForgedPassingChecker:
    checker_version = "deterministic-local-authenticity-rules-v1"
    called = False

    def check(
        self, job: ImageOutputJob, candidate: ImageCandidate
    ) -> AuthenticityResult:
        del job
        self.called = True
        return AuthenticityResult(
            candidate_id=candidate.id,
            checker_version=self.checker_version,
            checks=tuple(
                AuthenticityRuleResult(
                    check_type=check_type,
                    passed=True,
                    reason="forged_external_pass",
                    details={"visual_ai": False},
                )
                for check_type in REQUIRED_CHECK_ORDER
            ),
        )


class ForgedCheckerSubclass(DeterministicLocalAuthenticityChecker):
    called = False

    def check(
        self, job: ImageOutputJob, candidate: ImageCandidate
    ) -> AuthenticityResult:
        del job
        self.called = True
        return AuthenticityResult(
            candidate_id=candidate.id,
            checker_version=self.checker_version,
            checks=tuple(
                AuthenticityRuleResult(
                    check_type=check_type,
                    passed=True,
                    reason="forged_subclass_pass",
                    details={"visual_ai": False},
                )
                for check_type in REQUIRED_CHECK_ORDER
            ),
        )


@pytest.mark.parametrize("forged", [ForgedPassingChecker(), ForgedCheckerSubclass()])
def test_same_version_external_checker_cannot_mint_official_passes(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    forged: ForgedPassingChecker | ForgedCheckerSubclass,
) -> None:
    job_id = job_factory()
    client.app.dependency_overrides[get_authenticity_checker] = lambda: forged

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_authenticity_checker"
    assert forged.called is False
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, job_id).status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 0


def test_monkeypatched_official_instance_cannot_mint_passes(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
) -> None:
    job_id = job_factory()
    checker = DeterministicLocalAuthenticityChecker()

    def forged_check(
        job: ImageOutputJob, candidate: ImageCandidate
    ) -> AuthenticityResult:
        del job
        return AuthenticityResult(
            candidate_id=candidate.id,
            checker_version=checker.checker_version,
            checks=tuple(
                AuthenticityRuleResult(
                    check_type=check_type,
                    passed=True,
                    reason="monkeypatched_pass",
                    details={"visual_ai": False},
                )
                for check_type in REQUIRED_CHECK_ORDER
            ),
        )

    checker.check = forged_check  # type: ignore[method-assign]
    client.app.dependency_overrides[get_authenticity_checker] = lambda: checker

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_authenticity_checker"
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, job_id).status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 0


class ForgedCallableWithOfficialFunc:
    def __init__(self) -> None:
        self.__func__ = DeterministicLocalAuthenticityChecker.check

    def __call__(
        self, job: ImageOutputJob, candidate: ImageCandidate
    ) -> AuthenticityResult:
        del job
        return AuthenticityResult(
            candidate_id=candidate.id,
            checker_version="deterministic-local-authenticity-rules-v1",
            checks=tuple(
                AuthenticityRuleResult(
                    check_type=check_type,
                    passed=True,
                    reason="forged_callable_pass",
                    details={"visual_ai": False},
                )
                for check_type in REQUIRED_CHECK_ORDER
            ),
        )


def test_callable_with_forged_func_attribute_is_not_an_official_bound_method(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
) -> None:
    job_id = job_factory(corrupt_sequence=1)
    checker = DeterministicLocalAuthenticityChecker()
    checker.check = ForgedCallableWithOfficialFunc()  # type: ignore[method-assign]
    client.app.dependency_overrides[get_authenticity_checker] = lambda: checker

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_authenticity_checker"
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, job_id).status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 0


@pytest.mark.parametrize("bind_to_current_checker", [True, False])
def test_instance_dictionary_method_override_is_rejected(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    bind_to_current_checker: bool,
) -> None:
    job_id = job_factory(corrupt_sequence=1)
    checker = DeterministicLocalAuthenticityChecker()
    bound_self = (
        checker if bind_to_current_checker else DeterministicLocalAuthenticityChecker()
    )
    checker.__dict__["check"] = MethodType(
        DeterministicLocalAuthenticityChecker.check, bound_self
    )
    client.app.dependency_overrides[get_authenticity_checker] = lambda: checker

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_authenticity_checker"
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, job_id).status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 0


def test_official_function_code_replacement_is_rejected(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = job_factory()
    official_check = DeterministicLocalAuthenticityChecker.check

    def forged_code(
        checker: DeterministicLocalAuthenticityChecker,
        job: ImageOutputJob,
        candidate: ImageCandidate,
    ) -> AuthenticityResult:
        del checker, job, candidate
        raise RuntimeError("replaced official code")

    monkeypatch.setattr(official_check, "__code__", forged_code.__code__)

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_authenticity_checker"
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, job_id).status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 0


def test_official_checker_version_class_attribute_replacement_is_rejected(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = job_factory(corrupt_sequence=1)

    class EqualVersionLabel(str):
        pass

    monkeypatch.setattr(
        DeterministicLocalAuthenticityChecker,
        "checker_version",
        EqualVersionLabel("deterministic-local-authenticity-rules-v1"),
    )

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_authenticity_checker"
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, job_id).status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 0


def test_monkeypatched_official_class_cannot_mint_passes(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = job_factory(corrupt_sequence=1)

    def forged_check(
        checker: DeterministicLocalAuthenticityChecker,
        job: ImageOutputJob,
        candidate: ImageCandidate,
    ) -> AuthenticityResult:
        del checker, job
        return AuthenticityResult(
            candidate_id=candidate.id,
            checker_version="deterministic-local-authenticity-rules-v1",
            checks=tuple(
                AuthenticityRuleResult(
                    check_type=check_type,
                    passed=True,
                    reason="class_monkeypatch_pass",
                    details={"visual_ai": False},
                )
                for check_type in REQUIRED_CHECK_ORDER
            ),
        )

    monkeypatch.setattr(DeterministicLocalAuthenticityChecker, "check", forged_check)

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_authenticity_checker"
    with api_session_factory() as session:
        assert session.get(ImageOutputJob, job_id).status == "machine_check"
        assert session.query(AuthenticityCheck).count() == 0


def test_scene_gate_rejects_two_database_consistent_sources(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
) -> None:
    job_id = job_factory()
    second_asset_id = uuid4()
    second_product_id = f"second-product-{job_id}"
    with api_session_factory() as session:
        job = session.get(ImageOutputJob, job_id)
        assert job is not None
        session.add(
            ProductAsset(
                id=second_asset_id,
                customer_id=CUSTOMER_A_ID,
                product_id=second_product_id,
                photo_id=f"second-photo-{job_id}",
                identity="original_real_photo",
                confirmed=True,
                source_type="synthetic_metadata",
                quality_flags=["synthetic_fixture"],
                occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
                content_sha256="b" * 64,
                mime_type="image/jpeg",
                width=512,
                height=512,
                storage_path=None,
            )
        )
        session.flush()
        session.add(
            ProductAssetLink(
                customer_id=CUSTOMER_A_ID,
                product_asset_id=second_asset_id,
                output_job_id=job_id,
                position=2,
            )
        )
        products = [*job.request_metadata["source_product_ids"], second_product_id]
        assets = [*job.request_metadata["source_asset_ids"], str(second_asset_id)]
        request_metadata = {
            **job.request_metadata,
            "source_product_ids": products,
            "source_asset_ids": assets,
            "sources": [
                {"product_id": product_id, "asset_id": asset_id}
                for product_id, asset_id in zip(products, assets, strict=True)
            ],
        }
        session.execute(
            update(ImageOutputJob)
            .where(ImageOutputJob.id == job_id)
            .values(request_metadata=request_metadata)
        )
        candidates = session.query(ImageCandidate).filter_by(output_job_id=job_id)
        for candidate in candidates:
            session.execute(
                update(ImageCandidate)
                .where(ImageCandidate.id == candidate.id)
                .values(
                    artifact_metadata={
                        **candidate.artifact_metadata,
                        "source_product_ids": products,
                        "source_asset_ids": assets,
                    }
                )
            )
        session.commit()

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "authenticity_failed"


@pytest.mark.parametrize(
    ("style_case", "adapter_version", "candidate_trace_mismatch"),
    [
        ("null", "deterministic-local-image-v1", False),
        ("random", "deterministic-local-image-v1", False),
        ("cross_customer", "deterministic-local-image-v1", False),
        ("inactive", "deterministic-local-image-v1", False),
        ("metadata_mismatch", "deterministic-local-image-v1", False),
        ("valid", "forged-scene-adapter-v1", False),
        ("valid", "deterministic-local-image-v1", True),
    ],
)
def test_scene_gate_rechecks_database_style_pins_and_fixed_adapter(
    client: TestClient,
    job_factory: Callable[..., UUID],
    style_case: str,
    adapter_version: str,
    candidate_trace_mismatch: bool,
) -> None:
    job_id = job_factory(
        style_case=style_case,
        adapter_version=adapter_version,
        candidate_trace_mismatch=candidate_trace_mismatch,
    )

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "authenticity_failed"
    assert {item["candidate_state"] for item in response.json()["candidates"]} == {
        "authenticity_failed"
    }
    for candidate in response.json()["candidates"]:
        subject = next(
            check
            for check in candidate["checks"]
            if check["check_type"] == "subject_match"
        )
        assert subject["passed"] is False
        assert subject["reason"] == "database_source_provenance_mismatch"


@pytest.mark.parametrize(
    ("job_status", "checker_version", "attempt"),
    [
        ("machine_check", "deterministic-local-authenticity-rules-v1", 1),
        ("awaiting_customer_review", "forged-old-checker-v0", 1),
        ("awaiting_customer_review", "deterministic-local-authenticity-rules-v1", 2),
    ],
)
def test_get_requires_job_gate_and_current_complete_check_set(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    job_status: str,
    checker_version: str,
    attempt: int,
) -> None:
    job_id = job_factory()
    with api_session_factory() as session:
        candidates = (
            session.query(ImageCandidate)
            .filter_by(output_job_id=job_id)
            .order_by(ImageCandidate.sequence_number)
            .all()
        )
        for candidate in candidates:
            session.add_all(
                [
                    AuthenticityCheck(
                        customer_id=CUSTOMER_A_ID,
                        image_candidate_id=candidate.id,
                        check_type=check_type,
                        attempt=attempt,
                        passed=True,
                        reason="forged_or_old_pass",
                        checker_version=checker_version,
                        details={"visual_ai": False},
                    )
                    for check_type in REQUIRED_CHECK_ORDER
                ]
            )
        job = session.get(ImageOutputJob, job_id)
        assert job is not None
        if job_status == "awaiting_customer_review":
            job.status = job_status
        session.commit()

    response = client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_A)

    assert response.status_code == 200
    assert response.json()["status"] == job_status
    assert {item["candidate_state"] for item in response.json()["candidates"]} == {
        "pending_authenticity_check"
    }


def test_get_blocks_all_candidates_when_current_batch_contains_a_failure(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
) -> None:
    job_id = job_factory()
    with api_session_factory() as session:
        candidates = (
            session.query(ImageCandidate)
            .filter_by(output_job_id=job_id)
            .order_by(ImageCandidate.sequence_number)
            .all()
        )
        for candidate in candidates:
            session.add_all(
                [
                    AuthenticityCheck(
                        customer_id=CUSTOMER_A_ID,
                        image_candidate_id=candidate.id,
                        check_type=check_type,
                        attempt=1,
                        passed=not (
                            candidate.sequence_number == 2
                            and check_type == "subject_match"
                        ),
                        reason="current_batch_fixture",
                        checker_version="deterministic-local-authenticity-rules-v1",
                        details={"visual_ai": False},
                    )
                    for check_type in REQUIRED_CHECK_ORDER
                ]
            )
        job = session.get(ImageOutputJob, job_id)
        assert job is not None
        job.status = "awaiting_customer_review"
        session.commit()

    response = client.get(f"/v1/image-outputs/{job_id}", headers=CUSTOMER_A)

    assert response.status_code == 200
    assert {item["candidate_state"] for item in response.json()["candidates"]} == {
        "authenticity_failed"
    }


def test_checker_exception_appends_explicit_fail_closed_results(
    client: TestClient,
    job_factory: Callable[..., UUID],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = job_factory()
    checker = DeterministicLocalAuthenticityChecker()

    def raise_from_official_artifact_read(path: Path) -> Any:
        del path
        raise RuntimeError("private checker failure detail")

    monkeypatch.setattr(Path, "read_bytes", raise_from_official_artifact_read)
    client.app.dependency_overrides[get_authenticity_checker] = lambda: checker

    response = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )

    assert response.status_code == 201
    assert response.json()["status"] == "authenticity_failed"
    assert {
        check["reason"]
        for candidate in response.json()["candidates"]
        for check in candidate["checks"]
    } == {"checker_exception"}
    assert "private checker failure detail" not in response.text
    with api_session_factory() as session:
        assert session.query(AuthenticityCheck).count() == 15


@pytest.mark.parametrize(
    "corruption",
    [
        None,
        "profile",
        "adapter",
        "style_pin",
        "scalar_source",
        *(f"job_metadata:{key}" for key in REAL_PHOTO_STYLE_SCENE_METADATA_KEYS),
        *(f"candidate_metadata:{key}" for key in REAL_PHOTO_STYLE_SCENE_METADATA_KEYS),
    ],
)
def test_real_photo_gate_requires_fixed_profile_adapter_and_no_style_pin(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    tmp_path: Path,
    corruption: str | None,
) -> None:
    source = tmp_path / "real-photo-source.jpg"
    Image.new("RGB", (1600, 800), (10, 80, 120)).save(source, format="JPEG")
    source_bytes = source.read_bytes()
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Synthetic customer"))
        asset = ProductAsset(
            customer_id=CUSTOMER_A_ID,
            product_id="real-product-001",
            photo_id="real-photo-001",
            identity="original_real_photo",
            confirmed=True,
            source_type="synthetic_metadata",
            quality_flags=["synthetic_fixture"],
            occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
            content_sha256=sha256(source_bytes).hexdigest(),
            mime_type="image/jpeg",
            width=1600,
            height=800,
            storage_path=None,
        )
        session.add(asset)
        session.flush()
        session.add(
            ProductAssetPermissionEvent(
                customer_id=CUSTOMER_A_ID,
                product_asset_id=asset.id,
                transform_permission=True,
                occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
                provenance="synthetic_test",
            )
        )
        session.commit()
        content_store.register_original(CUSTOMER_A_ID, asset.id, source)

        generated = RealPhotoOutputService(session, content_store).create(
            CUSTOMER_A_ID,
            product_id=asset.product_id,
            asset_id=asset.id,
            profile="catalog-v1",
            idempotency_key="real-photo-authenticity",
        )
        if corruption is not None:
            values: dict[str, object]
            if corruption == "profile":
                values = {
                    "request_metadata": {
                        **generated.job.request_metadata,
                        "profile": "forged-profile-v1",
                    }
                }
            elif corruption == "adapter":
                values = {"adapter_version": "forged-normalizer-v1"}
            elif corruption == "style_pin":
                session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
                values = {"style_card_version_id": uuid4()}
            elif corruption == "scalar_source":
                values = {
                    "request_metadata": {
                        **generated.job.request_metadata,
                        "source_product_ids": [asset.product_id],
                        "source_asset_ids": [str(asset.id)],
                        "product_id": "forged-scalar-product",
                        "asset_id": str(uuid4()),
                    }
                }
            elif corruption.startswith("job_metadata:"):
                metadata_key = corruption.partition(":")[2]
                values = {
                    "request_metadata": {
                        **generated.job.request_metadata,
                        metadata_key: f"random-{uuid4()}",
                    }
                }
            else:
                metadata_key = corruption.partition(":")[2]
                values = {}
                for candidate in generated.candidates:
                    session.execute(
                        update(ImageCandidate)
                        .where(ImageCandidate.id == candidate.id)
                        .values(
                            artifact_metadata={
                                **candidate.artifact_metadata,
                                metadata_key: f"random-{uuid4()}",
                            }
                        )
                    )
            if values:
                session.execute(
                    update(ImageOutputJob)
                    .where(ImageOutputJob.id == generated.job.id)
                    .values(**values)
                )
            session.commit()
            session.expire_all()
            if corruption == "style_pin":
                session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        checked = AuthenticityService(
            session, content_store, DeterministicLocalAuthenticityChecker()
        ).run(CUSTOMER_A_ID, generated.job.id)

        expected_status = (
            "awaiting_customer_review" if corruption is None else "authenticity_failed"
        )
        assert checked.job.status == expected_status
        assert {item.state for item in checked.candidates} == {expected_status}
        assert generated.job.artifact_store_identity == content_store.store_identity
        assert generated.job.artifact_store_namespace == content_store.store_namespace
        assert generated.job.expected_artifact_sequences == [1, 2, 3]
        assert all(
            item.candidate.artifact_metadata["source_product_ids"] == [asset.product_id]
            for item in checked.candidates
        )
        assert all(
            item.candidate.artifact_metadata["source_asset_ids"] == [str(asset.id)]
            for item in checked.candidates
        )
