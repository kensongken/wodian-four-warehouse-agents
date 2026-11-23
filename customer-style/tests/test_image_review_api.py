"""Customer review and immutable real/style image association contracts."""

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Lock
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from opc_style.api.dependencies import get_session
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.domain.authenticity import (
    DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION,
    REQUIRED_AUTHENTICITY_CHECKS,
)
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    AuthenticityCheck,
    Customer,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    ProductAsset,
    ProductAssetLink,
    ProductAssetPermissionEvent,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
)
from opc_style.services.image_jobs import _forget_local_store
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _head_revision(config: Config) -> str:
    return ScriptDirectory.from_config(config).get_current_head()


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    database_url = f"sqlite:///{tmp_path / 'image-reviews.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    engine.dispose()


@pytest.fixture
def content_store(
    tmp_path: Path,
) -> Generator[LocalSyntheticAssetStore, None, None]:
    with LocalSyntheticAssetStore(tmp_path / "review-assets") as store:
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
def style_factory(
    api_session_factory: sessionmaker[Session],
) -> Callable[[UUID], tuple[SceneStyle, StyleCardVersion]]:
    def create_style(
        customer_id: UUID = CUSTOMER_A_ID,
    ) -> tuple[SceneStyle, StyleCardVersion]:
        with api_session_factory() as session:
            if session.get(Customer, customer_id) is None:
                session.add(Customer(id=customer_id, display_name="Synthetic customer"))
                session.flush()
            main_card = StyleCard(customer_id=customer_id, kind="main", active=True)
            session.add(main_card)
            session.flush()
            main_version = StyleCardVersion(
                customer_id=customer_id,
                style_card_id=main_card.id,
                version=1,
                content={"tone": ["synthetic"]},
            )
            session.add(main_version)
            session.flush()
            scene_id = uuid4()
            scene_card = StyleCard(
                id=scene_id, customer_id=customer_id, kind="scene", active=True
            )
            session.add(scene_card)
            session.flush()
            scene = SceneStyle(
                id=scene_id,
                customer_id=customer_id,
                style_card_id=scene_card.id,
                name=f"Synthetic scene {scene_id}",
                active=True,
                rules={},
            )
            scene_version = StyleCardVersion(
                customer_id=customer_id,
                style_card_id=scene_card.id,
                version=1,
                content={
                    "main_style_card_version_id": str(main_version.id),
                    "source": "synthetic_test",
                },
            )
            session.add_all([scene, scene_version])
            session.commit()
            session.refresh(scene)
            session.refresh(scene_version)
            session.expunge(scene)
            session.expunge(scene_version)
            return scene, scene_version

    return create_style


@pytest.fixture
def asset_factory(
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    tmp_path: Path,
) -> Callable[..., ProductAsset]:
    def create_asset(
        *, customer_id: UUID = CUSTOMER_A_ID, product_id: str = "product-001"
    ) -> ProductAsset:
        source = tmp_path / f"source-{uuid4()}.jpg"
        Image.new("RGB", (1200, 800), (18, 96, 144)).save(source, format="JPEG")
        payload = source.read_bytes()
        with api_session_factory() as session:
            if session.get(Customer, customer_id) is None:
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
                occurred_at=NOW,
                content_sha256=sha256(payload).hexdigest(),
                mime_type="image/jpeg",
                width=1200,
                height=800,
                storage_path=None,
            )
            session.add(asset)
            session.flush()
            session.add(
                ProductAssetPermissionEvent(
                    customer_id=customer_id,
                    product_asset_id=asset.id,
                    transform_permission=True,
                    occurred_at=NOW,
                    provenance="synthetic_test",
                )
            )
            session.commit()
            session.refresh(asset)
            session.expunge(asset)
        content_store.register_original(customer_id, asset.id, source)
        return asset

    return create_asset


def _generation_request(
    assets: list[ProductAsset],
    scene: SceneStyle,
    scene_version: StyleCardVersion,
    *,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        "sources": [
            {"product_id": asset.product_id, "asset_id": str(asset.id)}
            for asset in assets
        ],
        "scene_style_id": str(scene.id),
        "style_card_version_id": str(scene_version.id),
        "output_ratio": "1:1",
        "idempotency_key": idempotency_key,
    }


def _create_machine_passed_job(
    client: TestClient,
    assets: list[ProductAsset],
    scene: SceneStyle,
    scene_version: StyleCardVersion,
) -> tuple[UUID, list[UUID]]:
    endpoint = "scene" if len(assets) == 1 else "multi-product"
    generated = client.post(
        f"/v1/image-outputs/{endpoint}",
        headers=CUSTOMER_A,
        json=_generation_request(
            assets, scene, scene_version, idempotency_key=f"generate-{uuid4()}"
        ),
    )
    assert generated.status_code == 201, generated.text
    job_id = UUID(generated.json()["id"])
    checked = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )
    assert checked.status_code == 201, checked.text
    assert checked.json()["status"] == "awaiting_customer_review"
    return job_id, [UUID(item["id"]) for item in checked.json()["candidates"]]


def _review_request(
    decision: str = "approved", *, key: str = "review-request-001"
) -> dict[str, object]:
    return {"decision": decision, "reason": None, "idempotency_key": key}


def test_review_api_rejects_uncontrolled_fidelity_flag_before_lookup(
    client: TestClient,
) -> None:
    response = client.post(
        f"/v1/image-outputs/{uuid4()}/candidates/{uuid4()}/review",
        headers=CUSTOMER_A,
        json={
            **_review_request(key="bad-fidelity-flag"),
            "fidelity_state": "red",
            "fidelity_flags": ["provider_internal_message"],
        },
    )
    assert response.status_code == 422


def test_approved_multi_product_candidate_creates_dual_image_links_without_publish(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    assets = [
        asset_factory(product_id="product-zeta"),
        asset_factory(product_id="product-alpha"),
    ]
    job_id, candidate_ids = _create_machine_passed_job(
        client, assets, scene, scene_version
    )

    response = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[1]}/review",
        headers=CUSTOMER_A,
        json=_review_request(),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["job_id"] == str(job_id)
    assert body["candidate_id"] == str(candidate_ids[1])
    assert body["decision"] == "approved"
    assert body["job_status"] == "customer_approved"
    assert body["candidate_state"] == "customer_approved"
    assert body["image_kind"] == "style_image"
    assert body["customer_approved"] is True
    assert body["publishable"] is False
    assert body["source_product_ids"] == [item.product_id for item in assets]
    assert body["source_asset_ids"] == [str(item.id) for item in assets]
    assert len(body["product_associations"]) == 2
    assert "published" not in body
    assert "publish_action" not in body
    for association, asset in zip(body["product_associations"], assets, strict=True):
        assert association["product_id"] == asset.product_id
        assert association["approved_image"]["image_kind"] == "style_image"
        assert association["approved_image"]["candidate_id"] == str(candidate_ids[1])
        assert association["approved_image"]["publishable"] is False
        assert association["real_photo"]["image_kind"] == "real_photo"
        assert association["real_photo"]["asset_id"] == str(asset.id)
        assert association["real_photo"]["content_sha256"] == asset.content_sha256

    replay = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[1]}/review",
        headers=CUSTOMER_A,
        json=_review_request(),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == body

    for asset in assets:
        default_view = client.get(
            f"/v1/products/{asset.product_id}/images", headers=CUSTOMER_A
        )
        real_view = client.get(
            f"/v1/products/{asset.product_id}/images?view=real", headers=CUSTOMER_A
        )
        assert default_view.status_code == 200, default_view.text
        assert default_view.json()["requested_view"] == "default"
        assert default_view.json()["resolved_view"] == "style_image"
        assert default_view.json()["images"][0]["candidate_id"] == str(candidate_ids[1])
        assert real_view.status_code == 200, real_view.text
        assert real_view.json()["requested_view"] == "real"
        assert real_view.json()["resolved_view"] == "real_photo"
        assert {item["asset_id"] for item in real_view.json()["images"]} == {
            str(asset.id)
        }
        assert all(
            item["image_kind"] == "real_photo" for item in real_view.json()["images"]
        )

    with api_session_factory() as session:
        assert session.get(ImageOutputJob, job_id).status == "customer_approved"
        assert session.query(ImageReview).count() == 1
        assert session.query(ProductAssetLink).count() == 2
        association_rows = session.execute(
            text(
                "SELECT product_id, source_asset_id, image_candidate_id, image_kind "
                "FROM approved_image_associations ORDER BY product_id"
            )
        ).all()
        assert association_rows == [
            (asset.product_id, str(asset.id), str(candidate_ids[1]), "style_image")
            for asset in sorted(assets, key=lambda item: item.product_id)
        ]
        stored_original_facts = [
            (item.id, item.content_sha256, item.identity)
            for item in session.query(ProductAsset).order_by(ProductAsset.product_id)
        ]
        expected_original_facts = [
            (item.id, item.content_sha256, item.identity)
            for item in sorted(assets, key=lambda item: item.product_id)
        ]
        assert stored_original_facts == expected_original_facts
        assert (
            session.query(AuditEvent)
            .filter_by(event_type="image_output.customer_reviewed")
            .count()
            == 1
        )


def test_approved_style_default_view_fails_closed_when_artifact_is_missing(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory(product_id="missing-approved-style")
    job_id, candidate_ids = _create_machine_passed_job(
        client, [asset], scene, scene_version
    )
    approved = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="approve-before-delete"),
    )
    assert approved.status_code == 201, approved.text
    with api_session_factory() as session:
        candidate = session.get(ImageCandidate, candidate_ids[0])
        assert candidate is not None
        Path(candidate.artifact_path).unlink()

    response = client.get(
        f"/v1/products/{asset.product_id}/images", headers=CUSTOMER_A
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "review_artifact_outcome_uncertain",
        "retryable": True,
    }


def test_database_rejects_approved_association_outside_candidate_job_sources(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    source = asset_factory(product_id="bound-source")
    unrelated = asset_factory(product_id="unrelated-source")
    job_id, candidate_ids = _create_machine_passed_job(
        client, [source], scene, scene_version
    )
    reviewed = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="source-binding-review"),
    )
    assert reviewed.status_code == 201, reviewed.text

    with api_session_factory() as session:
        review = session.query(ImageReview).one()
        source_link = (
            session.query(ProductAssetLink).filter_by(output_job_id=job_id).one()
        )
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO approved_image_associations "
                    "(id, customer_id, image_review_id, image_candidate_id, "
                    "output_job_id, product_asset_link_id, source_asset_id, "
                    "product_id, image_kind, created_at) VALUES "
                    "(:id, :customer_id, :review_id, :candidate_id, :job_id, "
                    ":link_id, :asset_id, :product_id, 'style_image', :now)"
                ),
                {
                    "id": str(uuid4()),
                    "customer_id": str(CUSTOMER_A_ID),
                    "review_id": str(review.id),
                    "candidate_id": str(candidate_ids[0]),
                    "job_id": str(job_id),
                    "link_id": str(source_link.id),
                    "asset_id": str(unrelated.id),
                    "product_id": unrelated.product_id,
                    "now": NOW.isoformat(),
                },
            )


@pytest.mark.parametrize(
    "decision", ["not_my_style", "product_detail_inaccurate", "rejected"]
)
def test_negative_decisions_are_terminal_rejections_without_associations(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    decision: str,
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory()
    job_id, candidate_ids = _create_machine_passed_job(
        client, [asset], scene, scene_version
    )

    response = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(decision, key=f"negative-{decision}"),
    )

    assert response.status_code == 201, response.text
    assert response.json()["decision"] == decision
    assert response.json()["job_status"] == "customer_rejected"
    assert response.json()["candidate_state"] == "customer_rejected"
    assert response.json()["customer_approved"] is False
    assert response.json()["publishable"] is False
    assert response.json()["product_associations"] == []
    with api_session_factory() as session:
        assert session.query(ImageReview).count() == 1
        assert (
            session.execute(
                text("SELECT count(*) FROM approved_image_associations")
            ).scalar_one()
            == 0
        )


def test_only_owned_candidate_on_matching_awaiting_job_can_be_decided(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    first_asset = asset_factory(product_id="first")
    second_asset = asset_factory(product_id="second")
    first_job, first_candidates = _create_machine_passed_job(
        client, [first_asset], scene, scene_version
    )
    second_job, second_candidates = _create_machine_passed_job(
        client, [second_asset], scene, scene_version
    )

    foreign = client.post(
        f"/v1/image-outputs/{first_job}/candidates/{first_candidates[0]}/review",
        headers=CUSTOMER_B,
        json=_review_request(key="foreign"),
    )
    mismatched = client.post(
        f"/v1/image-outputs/{first_job}/candidates/{second_candidates[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="mismatched"),
    )
    with api_session_factory() as session:
        session.execute(
            update(ImageOutputJob)
            .where(ImageOutputJob.id == second_job)
            .values(status="disabled", failure_reason="synthetic_disabled")
        )
        session.commit()
    failed_job = client.post(
        f"/v1/image-outputs/{second_job}/candidates/{second_candidates[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="failed-job"),
    )

    assert foreign.status_code == 404
    assert mismatched.status_code == 404
    assert failed_job.status_code == 409
    assert failed_job.json()["error"]["code"] == "review_job_not_awaiting_customer"


def test_missing_or_legacy_machine_checks_never_count_as_customer_approval(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory()
    generated = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_generation_request(
            [asset], scene, scene_version, idempotency_key="unchecked-job"
        ),
    )
    assert generated.status_code == 201
    job_id = UUID(generated.json()["id"])
    candidate_id = UUID(generated.json()["candidates"][0]["id"])

    missing = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_id}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="missing-checks"),
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "review_job_not_awaiting_customer"

    with api_session_factory() as session:
        for check_type in REQUIRED_AUTHENTICITY_CHECKS:
            session.add(
                AuthenticityCheck(
                    customer_id=CUSTOMER_A_ID,
                    image_candidate_id=candidate_id,
                    check_type=check_type,
                    attempt=1,
                    passed=True,
                    reason="forged_legacy_pass",
                    checker_version="legacy-checker-v0",
                    details={"visual_ai": False},
                )
            )
        session.flush()
        session.execute(
            update(ImageOutputJob)
            .where(ImageOutputJob.id == job_id)
            .values(status="awaiting_customer_review", failure_reason=None)
        )
        session.commit()

    legacy = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_id}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="legacy-checks"),
    )
    assert legacy.status_code == 409
    assert legacy.json()["error"]["code"] == "candidate_machine_check_not_current_pass"
    with api_session_factory() as session:
        assert session.query(ImageReview).count() == 0


def test_direct_sql_forged_current_checks_and_status_cannot_authorize_review(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    """Rows that resemble Task 5 output are not its durable completion fact."""
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory(product_id="forged-machine-pass")
    generated = client.post(
        "/v1/image-outputs/scene",
        headers=CUSTOMER_A,
        json=_generation_request(
            [asset], scene, scene_version, idempotency_key="forged-complete-job"
        ),
    )
    assert generated.status_code == 201, generated.text
    job_id = UUID(generated.json()["id"])
    candidate_ids = [UUID(item["id"]) for item in generated.json()["candidates"]]

    with api_session_factory() as session:
        for candidate_id in candidate_ids:
            for check_type in REQUIRED_AUTHENTICITY_CHECKS:
                session.add(
                    AuthenticityCheck(
                        customer_id=CUSTOMER_A_ID,
                        image_candidate_id=candidate_id,
                        check_type=check_type,
                        attempt=1,
                        passed=True,
                        reason="passed",
                        checker_version=(
                            DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION
                        ),
                        details={"visual_ai": False},
                    )
                )
        session.flush()
        session.execute(
            update(ImageOutputJob)
            .where(ImageOutputJob.id == job_id)
            .values(status="awaiting_customer_review", failure_reason=None)
        )
        session.commit()

    response = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="forged-completion-review"),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == (
        "candidate_machine_check_not_current_pass"
    )
    with api_session_factory() as session:
        assert session.query(ImageReview).count() == 0


@pytest.mark.parametrize("corruption", ["missing_audit", "digest_changed"])
def test_review_requires_matching_completion_seal_and_audit_provenance(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    corruption: str,
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory(product_id=f"sealed-{corruption}")
    job_id, candidate_ids = _create_machine_passed_job(
        client, [asset], scene, scene_version
    )
    with api_session_factory() as session:
        if corruption == "missing_audit":
            session.execute(text("DROP TRIGGER trg_audit_events_no_delete"))
            session.execute(
                text(
                    "DELETE FROM audit_events "
                    "WHERE event_type='image_output.authenticity_completed'"
                )
            )
        else:
            session.execute(
                text("DROP TRIGGER trg_authenticity_completions_no_update")
            )
            session.execute(
                text(
                    "UPDATE authenticity_completions "
                    "SET candidate_batch_digest=:digest WHERE output_job_id=:job_id"
                ),
                {"digest": "0" * 64, "job_id": str(job_id)},
            )
        session.commit()

    response = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(key=f"broken-seal-{corruption}"),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == (
        "candidate_machine_check_not_current_pass"
    )


def test_same_review_replay_is_200_and_any_second_decision_conflicts(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory()
    job_id, candidate_ids = _create_machine_passed_job(
        client, [asset], scene, scene_version
    )
    url = f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review"

    created = client.post(
        url, headers=CUSTOMER_A, json=_review_request(key="stable-review-key")
    )
    replay = client.post(
        url, headers=CUSTOMER_A, json=_review_request(key="stable-review-key")
    )
    conflicting = client.post(
        url,
        headers=CUSTOMER_A,
        json=_review_request("rejected", key="second-review-key"),
    )

    assert created.status_code == 201, created.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == created.json()
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "candidate_review_already_decided"
    with api_session_factory() as session:
        assert session.query(ImageReview).count() == 1


def test_one_idempotency_key_cannot_decide_another_candidate(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory()
    job_id, candidate_ids = _create_machine_passed_job(
        client, [asset], scene, scene_version
    )
    first = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request("rejected", key="one-key"),
    )
    second = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[1]}/review",
        headers=CUSTOMER_A,
        json=_review_request("rejected", key="one-key"),
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "review_idempotency_conflict"


def test_concurrent_conflicting_reviews_create_exactly_one_decision(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory()
    job_id, candidate_ids = _create_machine_passed_job(
        client, [asset], scene, scene_version
    )
    url = f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review"
    barrier = Barrier(2)

    def submit(decision: str) -> tuple[int, dict[str, Any]]:
        barrier.wait(timeout=5)
        response = client.post(
            url,
            headers=CUSTOMER_A,
            json=_review_request(decision, key=f"concurrent-{decision}"),
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["approved", "rejected"]))

    assert sorted(status for status, _ in results) == [201, 409]
    with api_session_factory() as session:
        assert session.query(ImageReview).count() == 1
        assert session.get(ImageOutputJob, job_id).status in {
            "customer_approved",
            "customer_rejected",
        }


def test_commit_ack_loss_recovers_the_durable_review_as_200(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory()
    job_id, candidate_ids = _create_machine_passed_job(
        client, [asset], scene, scene_version
    )
    original_commit = Session.commit
    lock = Lock()
    lost = False

    def commit_then_lose_ack(session: Session) -> None:
        nonlocal lost
        is_review_commit = any(
            isinstance(item, AuditEvent)
            and item.event_type == "image_output.customer_reviewed"
            for item in session.new
        )
        original_commit(session)
        if is_review_commit:
            with lock:
                if not lost:
                    lost = True
                    raise RuntimeError("synthetic review commit acknowledgement loss")

    monkeypatch.setattr(Session, "commit", commit_then_lose_ack)
    response = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="ack-loss-review"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "approved"
    with api_session_factory() as session:
        assert session.query(ImageReview).count() == 1
        assert session.get(ImageOutputJob, job_id).status == "customer_approved"


@pytest.mark.parametrize("failure", ["restart", "artifact_replaced"])
def test_review_fails_closed_without_current_exact_artifact_proof(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
    content_store: LocalSyntheticAssetStore,
    failure: str,
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory()
    job_id, candidate_ids = _create_machine_passed_job(
        client, [asset], scene, scene_version
    )
    if failure == "restart":
        _forget_local_store(job_id, content_store)
    else:
        with api_session_factory() as session:
            candidate = session.get(ImageCandidate, candidate_ids[0])
            assert candidate is not None
            Path(candidate.artifact_path).write_bytes(b"same-uid replacement")

    response = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_ids[0]}/review",
        headers=CUSTOMER_A,
        json=_review_request(key=f"fail-closed-{failure}"),
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "review_artifact_outcome_uncertain",
        "retryable": True,
    }
    with api_session_factory() as session:
        assert session.query(ImageReview).count() == 0
        assert session.get(ImageOutputJob, job_id).status == "awaiting_customer_review"


def test_real_photo_approval_keeps_the_original_source_link(
    client: TestClient,
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    asset = asset_factory(product_id="truthful-product")
    generated = client.post(
        "/v1/image-outputs/real-photo",
        headers=CUSTOMER_A,
        json={
            "product_id": asset.product_id,
            "asset_id": str(asset.id),
            "profile": "catalog-v1",
            "idempotency_key": "real-photo-for-review",
        },
    )
    assert generated.status_code == 201, generated.text
    job_id = UUID(generated.json()["id"])
    checked = client.post(
        f"/v1/image-outputs/{job_id}/authenticity-checks", headers=CUSTOMER_A
    )
    assert checked.status_code == 201, checked.text
    candidate_id = UUID(checked.json()["candidates"][0]["id"])

    response = client.post(
        f"/v1/image-outputs/{job_id}/candidates/{candidate_id}/review",
        headers=CUSTOMER_A,
        json=_review_request(key="approve-real-photo"),
    )

    assert response.status_code == 201, response.text
    assert response.json()["image_kind"] == "real_photo"
    association = response.json()["product_associations"][0]
    assert association["approved_image"]["image_kind"] == "real_photo"
    assert association["real_photo"]["asset_id"] == str(asset.id)
    with api_session_factory() as session:
        source_links = (
            session.query(ProductAssetLink).filter_by(output_job_id=job_id).all()
        )
        assert [item.product_asset_id for item in source_links] == [asset.id]
        row = session.execute(
            text(
                "SELECT source_asset_id, image_candidate_id, image_kind "
                "FROM approved_image_associations"
            )
        ).one()
        assert row == (str(asset.id), str(candidate_id), "real_photo")


def test_explicit_real_view_never_masquerades_ai_asset_as_real(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
) -> None:
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Synthetic customer"))
        session.flush()
        session.add(
            ProductAsset(
                customer_id=CUSTOMER_A_ID,
                product_id="ai-only-product",
                photo_id="ai-only-photo",
                identity="ai_style_generated",
                confirmed=True,
                source_type="synthetic_metadata",
                quality_flags=["synthetic_fixture"],
                occurred_at=NOW,
                content_sha256="f" * 64,
                mime_type="image/jpeg",
                width=100,
                height=100,
                storage_path=None,
            )
        )
        session.commit()

    response = client.get(
        "/v1/products/ai-only-product/images?view=real", headers=CUSTOMER_A
    )

    assert response.status_code == 404


def test_explicit_real_view_only_returns_confirmed_original_real_photos(
    client: TestClient,
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    original = asset_factory(product_id="mixed-product")
    with api_session_factory() as session:
        session.add(
            ProductAsset(
                customer_id=CUSTOMER_A_ID,
                product_id="mixed-product",
                photo_id="mixed-ai-photo",
                identity="ai_style_generated",
                confirmed=True,
                source_type="synthetic_metadata",
                quality_flags=["synthetic_fixture"],
                occurred_at=NOW,
                content_sha256="e" * 64,
                mime_type="image/jpeg",
                width=100,
                height=100,
                storage_path=None,
            )
        )
        session.commit()

    response = client.get(
        "/v1/products/mixed-product/images?view=real", headers=CUSTOMER_A
    )

    assert response.status_code == 200, response.text
    assert [item["asset_id"] for item in response.json()["images"]] == [
        str(original.id)
    ]


def test_review_schema_rejects_unknown_decision_fields_and_bad_keys(
    client: TestClient,
) -> None:
    url = f"/v1/image-outputs/{uuid4()}/candidates/{uuid4()}/review"
    unknown_decision = client.post(
        url,
        headers=CUSTOMER_A,
        json=_review_request("machine_passed", key="bad-decision"),
    )
    unknown_field = client.post(
        url,
        headers=CUSTOMER_A,
        json={**_review_request(), "publish": True},
    )
    bad_key = client.post(
        url,
        headers=CUSTOMER_A,
        json=_review_request(key="contains whitespace"),
    )
    assert unknown_decision.status_code == 422
    assert unknown_field.status_code == 422
    assert bad_key.status_code == 422


def test_0009_image_association_migration_round_trip_and_guards(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'review-migration.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    assert "approved_image_associations" in inspect(engine).get_table_names()
    indexes = inspect(engine).get_indexes("approved_image_associations")
    assert any(
        item["unique"]
        and item["column_names"] == ["image_candidate_id", "source_asset_id"]
        for item in indexes
    )
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name IN ('approved_image_associations', 'image_reviews')"
                )
            )
        }
    assert {
        "trg_approved_image_associations_no_update",
        "trg_approved_image_associations_no_delete",
        "trg_approved_image_associations_approved_review_only",
        "trg_approved_image_associations_source_job_only",
        "trg_image_reviews_decision_insert",
    } <= triggers
    with engine.connect() as connection:
        completion_triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'authenticity_completions'"
                )
            )
        }
    assert {
        "trg_authenticity_completions_no_update",
        "trg_authenticity_completions_no_delete",
        "trg_authenticity_completions_complete_batch_only",
    } <= completion_triggers
    engine.dispose()


def test_0010_fails_before_any_ddl_when_0009_has_legacy_reviews(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'review-legacy-guard.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0009_customer_image_review")
    engine = create_database_engine(database_url)
    customer_id = str(CUSTOMER_A_ID)
    job_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    candidate_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    now = NOW.isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Synthetic', :now)"
            ),
            {"id": customer_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO image_output_jobs "
                "(id, customer_id, output_type, output_ratio, adapter_version, "
                "status, failure_reason, request_metadata, idempotency_key, "
                "created_at, updated_at) VALUES "
                "(:id, :customer_id, 'scene', '1:1', "
                "'deterministic-local-image-v1', 'customer_approved', NULL, "
                "'{}', 'legacy-review-job', :now, :now)"
            ),
            {"id": job_id, "customer_id": customer_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO image_candidates "
                "(id, customer_id, output_job_id, sequence_number, identity, "
                "artifact_path, content_sha256, mime_type, width, height, "
                "adapter_version, metadata, created_at) VALUES "
                "(:id, :customer_id, :job_id, 1, 'synthetic_style_image_candidate', "
                "'opaque', :digest, 'image/jpeg', 10, 10, "
                "'deterministic-local-image-v1', '{}', :now)"
            ),
            {
                "id": candidate_id,
                "customer_id": customer_id,
                "job_id": job_id,
                "digest": "b" * 64,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO image_reviews "
                "(id, customer_id, image_candidate_id, decision, reason, "
                "idempotency_key, created_at) VALUES "
                "('dddddddd-dddd-dddd-dddd-dddddddddddd', :customer_id, "
                ":candidate_id, 'approved', NULL, 'legacy-review', :now)"
            ),
            {"customer_id": customer_id, "candidate_id": candidate_id, "now": now},
        )
    with engine.connect() as connection:
        before = connection.execute(
            text(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ).all()

    with pytest.raises(RuntimeError, match="legacy image reviews"):
        command.upgrade(config, "head")

    with engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ).all()
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0009_customer_image_review"
        )
    assert after == before
    engine.dispose()


def test_0010_upgrade_retries_after_supporting_index_was_partially_created(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'review-partial-retry.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0008_image_job_fencing_capability")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_product_assets_customer_id_product "
                "ON product_assets (customer_id, id, product_id)"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    assert {
        "approved_image_associations",
        "authenticity_completions",
    } <= set(inspect(engine).get_table_names())
    assert (
        engine.connect()
        .execute(text("SELECT version_num FROM alembic_version"))
        .scalar_one()
        == _head_revision(config)
    )
    engine.dispose()

    command.downgrade(config, "0008_image_job_fencing_capability")
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    assert "approved_image_associations" in inspect(engine).get_table_names()
    engine.dispose()


def test_0010_refuses_to_drop_or_mutate_durable_approved_association(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'review-downgrade-guard.db'}"
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    customer_id = str(CUSTOMER_A_ID)
    asset_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    job_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    candidate_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    review_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    association_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    source_link_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    now = "2026-08-31T12:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO customers (id, display_name, created_at) "
                "VALUES (:id, 'Synthetic', :now)"
            ),
            {"id": customer_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO product_assets "
                "(id, customer_id, product_id, photo_id, identity, confirmed, "
                "source_type, quality_flags, occurred_at, content_sha256, "
                "mime_type, width, height, storage_path, created_at) VALUES "
                "(:id, :customer_id, 'product-guard', 'photo-guard', "
                "'original_real_photo', 1, 'synthetic_metadata', '[]', :now, "
                ":digest, 'image/jpeg', 10, 10, NULL, :now)"
            ),
            {
                "id": asset_id,
                "customer_id": customer_id,
                "now": now,
                "digest": "a" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO image_output_jobs "
                "(id, customer_id, output_type, output_ratio, adapter_version, "
                "status, failure_reason, request_metadata, idempotency_key, "
                "created_at, updated_at) VALUES "
                "(:id, :customer_id, 'scene', '1:1', "
                "'deterministic-local-image-v1', 'customer_approved', NULL, "
                "'{}', 'review-migration-guard', :now, :now)"
            ),
            {"id": job_id, "customer_id": customer_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO product_asset_links "
                "(id, customer_id, product_asset_id, output_job_id, position, "
                "created_at) VALUES (:id, :customer_id, :asset_id, :job_id, 1, :now)"
            ),
            {
                "id": source_link_id,
                "customer_id": customer_id,
                "asset_id": asset_id,
                "job_id": job_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO image_candidates "
                "(id, customer_id, output_job_id, sequence_number, identity, "
                "artifact_path, content_sha256, mime_type, width, height, "
                "adapter_version, metadata, created_at) VALUES "
                "(:id, :customer_id, :job_id, 1, "
                "'synthetic_style_image_candidate', 'opaque-test-path', :digest, "
                "'image/jpeg', 10, 10, 'deterministic-local-image-v1', '{}', :now)"
            ),
            {
                "id": candidate_id,
                "customer_id": customer_id,
                "job_id": job_id,
                "digest": "b" * 64,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO image_reviews "
                "(id, customer_id, image_candidate_id, decision, reason, "
                "idempotency_key, created_at) VALUES "
                "(:id, :customer_id, :candidate_id, 'approved', NULL, "
                "'review-guard', :now)"
            ),
            {
                "id": review_id,
                "customer_id": customer_id,
                "candidate_id": candidate_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO approved_image_associations "
                "(id, customer_id, image_review_id, image_candidate_id, "
                "output_job_id, product_asset_link_id, source_asset_id, product_id, "
                "image_kind, created_at) VALUES "
                "(:id, :customer_id, :review_id, :candidate_id, :job_id, :link_id, "
                ":asset_id, "
                "'product-guard', 'style_image', :now)"
            ),
            {
                "id": association_id,
                "customer_id": customer_id,
                "review_id": review_id,
                "candidate_id": candidate_id,
                "job_id": job_id,
                "link_id": source_link_id,
                "asset_id": asset_id,
                "now": now,
            },
        )

    unrelated_asset_id = "99999999-9999-9999-9999-999999999999"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO product_assets "
                "(id, customer_id, product_id, photo_id, identity, confirmed, "
                "source_type, quality_flags, occurred_at, content_sha256, "
                "mime_type, width, height, storage_path, created_at) VALUES "
                "(:id, :customer_id, 'unrelated-guard', 'unrelated-photo', "
                "'original_real_photo', 1, 'synthetic_metadata', '[]', :now, "
                ":digest, 'image/jpeg', 10, 10, NULL, :now)"
            ),
            {
                "id": unrelated_asset_id,
                "customer_id": customer_id,
                "now": now,
                "digest": "9" * 64,
            },
        )
    with (
        pytest.raises(IntegrityError, match="association_source_not_job_source"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO approved_image_associations "
                "(id, customer_id, image_review_id, image_candidate_id, "
                "output_job_id, product_asset_link_id, source_asset_id, product_id, "
                "image_kind, created_at) VALUES "
                "('88888888-8888-8888-8888-888888888888', :customer_id, "
                ":review_id, :candidate_id, :job_id, :link_id, :asset_id, "
                "'unrelated-guard', 'style_image', :now)"
            ),
            {
                "customer_id": customer_id,
                "review_id": review_id,
                "candidate_id": candidate_id,
                "job_id": job_id,
                "link_id": source_link_id,
                "asset_id": unrelated_asset_id,
                "now": now,
            },
        )

    with (
        pytest.raises(IntegrityError, match="approved_image_association_immutable"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE approved_image_associations SET image_kind='real_photo' "
                "WHERE id=:id"
            ),
            {"id": association_id},
        )
    with (
        pytest.raises(IntegrityError, match="approved_image_association_immutable"),
        engine.begin() as connection,
    ):
        connection.execute(
            text("DELETE FROM approved_image_associations WHERE id=:id"),
            {"id": association_id},
        )
    with pytest.raises(RuntimeError, match="customer-approved image associations"):
        command.downgrade(config, "0008_image_job_fencing_capability")

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0010_authenticity_completion"
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM approved_image_associations")
            ).scalar_one()
            == 1
        )
    engine.dispose()


def test_0010_refuses_downgrade_when_authenticity_completion_exists(
    client: TestClient,
    style_factory: Callable[[UUID], tuple[SceneStyle, StyleCardVersion]],
    asset_factory: Callable[..., ProductAsset],
    api_session_factory: sessionmaker[Session],
) -> None:
    scene, scene_version = style_factory(CUSTOMER_A_ID)
    asset = asset_factory(product_id="completion-downgrade-guard")
    job_id, _ = _create_machine_passed_job(client, [asset], scene, scene_version)
    bind = api_session_factory.kw["bind"]
    assert bind is not None
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", str(bind.url))

    with pytest.raises(RuntimeError, match="durable authenticity completions"):
        command.downgrade(config, "0009_customer_image_review")

    with api_session_factory() as session:
        assert session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0010_authenticity_completion"
        assert session.execute(
            text(
                "SELECT count(*) FROM authenticity_completions "
                "WHERE output_job_id=:job_id"
            ),
            {"job_id": str(job_id)},
        ).scalar_one() == 1
