"""HTTP contracts for confirmed, customer-owned product assets."""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from opc_style.db import create_database_engine
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    Base,
    Customer,
    ProductAsset,
    ProductAssetPermissionEvent,
)
from opc_style.repositories import ProductImageRepository

WORKTREE_ROOT = Path(__file__).resolve().parents[1]

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}
OCCURRED_AT = "2026-08-30T09:00:00Z"


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'asset-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(api_session_factory: sessionmaker[Session]) -> Generator[TestClient, None, None]:
    from opc_style.api.dependencies import get_session

    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def migrated_api_session_factory(
    tmp_path: Path,
) -> Generator[sessionmaker[Session], None, None]:
    database_url = f"sqlite:///{tmp_path / 'migrated-asset-api.db'}"
    config = Config(str(WORKTREE_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(WORKTREE_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    engine.dispose()


@pytest.fixture
def migrated_client(
    migrated_api_session_factory: sessionmaker[Session],
) -> Generator[TestClient, None, None]:
    from opc_style.api.dependencies import get_session

    app = create_app()

    def override_session() -> Generator[Session, None, None]:
        with migrated_api_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as test_client:
        yield test_client


def asset_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product_id": "product-001",
        "photo_id": "photo-001",
        "confirmed": True,
        "source_type": "synthetic_metadata",
        "quality_flags": ["focus_ok", "product_visible"],
        "occurred_at": OCCURRED_AT,
        "transform_permission": True,
        "content_sha256": "a" * 64,
        "mime_type": "image/jpeg",
        "width": 1200,
        "height": 900,
    }
    payload.update(overrides)
    return payload


def intake(
    client: TestClient,
    *,
    headers: dict[str, str] = CUSTOMER_A,
    expected_status: int = 201,
    **overrides: Any,
) -> dict[str, Any]:
    response = client.post(
        "/v1/product-assets", headers=headers, json=asset_payload(**overrides)
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def gate_concurrent_missing_asset_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make two intake attempts observe the same missing-photo state."""
    barrier = Barrier(2)
    original_get = ProductImageRepository.get_asset_by_photo

    def gated_get(
        repository: ProductImageRepository, customer_id: UUID, photo_id: str
    ) -> ProductAsset | None:
        asset = original_get(repository, customer_id, photo_id)
        if asset is None:
            barrier.wait(timeout=5)
        return asset

    monkeypatch.setattr(ProductImageRepository, "get_asset_by_photo", gated_get)


def test_confirmed_false_is_rejected_without_side_effects(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/v1/product-assets", headers=CUSTOMER_A, json=asset_payload(confirmed=False)
    )

    assert response.status_code == 422
    with api_session_factory() as session:
        assert session.query(Customer).count() == 0
        assert session.query(ProductAsset).count() == 0
        assert session.query(ProductAssetPermissionEvent).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_permission_false_is_audited_but_not_generation_ready(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    response = intake(client, transform_permission=False)

    assert response["identity"] == "original_real_photo"
    assert response["transform_permission"] is False
    assert response["generation_ready"] is False
    assert response["readiness_reasons"] == ["transform_permission_denied"]
    assert "storage_path" not in response

    with api_session_factory() as session:
        asset = session.query(ProductAsset).one()
        permission = session.query(ProductAssetPermissionEvent).one()
        audits = session.query(AuditEvent).order_by(AuditEvent.occurred_at).all()

    assert asset.confirmed is True
    assert asset.storage_path is None
    assert permission.product_asset_id == asset.id
    assert permission.transform_permission is False
    assert permission.provenance == "product_asset_intake"
    assert [event.event_type for event in audits] == [
        "product_asset.intaken",
        "product_asset.transform_permission_recorded",
    ]


def test_permission_true_is_generation_ready(client: TestClient) -> None:
    response = intake(client)

    assert response["transform_permission"] is True
    assert response["generation_ready"] is True
    assert response["readiness_reasons"] == []


def test_same_customer_photo_and_hash_replay_is_idempotent(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    first = intake(client)
    replay = intake(client, expected_status=200)

    assert replay == first
    with api_session_factory() as session:
        assert session.query(ProductAsset).count() == 1
        assert session.query(ProductAssetPermissionEvent).count() == 1
        assert session.query(AuditEvent).count() == 2


def test_equivalent_timezone_offsets_replay_as_one_utc_instant(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    first = intake(client, occurred_at="2026-08-30T17:00:00+08:00")
    replay = intake(client, expected_status=200, occurred_at=OCCURRED_AT)

    assert first["occurred_at"] == OCCURRED_AT
    assert replay["id"] == first["id"]
    with api_session_factory() as session:
        stored = session.query(ProductAsset).one()
    assert stored.occurred_at.isoformat() == "2026-08-30T09:00:00"


def test_same_photo_with_conflicting_hash_returns_nonretryable_conflict(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    intake(client)

    response = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(content_sha256="b" * 64),
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "product_asset_content_conflict", "retryable": False}
    }
    with api_session_factory() as session:
        assert session.query(ProductAsset).count() == 1
        assert session.query(ProductAssetPermissionEvent).count() == 1
        assert session.query(AuditEvent).count() == 2


def test_replay_with_changed_immutable_facts_conflicts(client: TestClient) -> None:
    intake(client)

    response = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(width=1201),
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "product_asset_facts_conflict", "retryable": False}
    }


def test_replay_cannot_rewrite_initial_permission_fact(client: TestClient) -> None:
    intake(client, transform_permission=False)

    response = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(transform_permission=True),
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "product_asset_permission_conflict", "retryable": False}
    }


def test_replay_compares_requested_permission_to_initial_not_latest_event(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    created = intake(client, transform_permission=False)
    with api_session_factory() as session:
        session.add(
            ProductAssetPermissionEvent(
                customer_id=CUSTOMER_A_ID,
                product_asset_id=UUID(created["id"]),
                transform_permission=True,
                occurred_at=datetime(2026, 8, 30, 10, tzinfo=UTC),
                provenance="synthetic_permission_review",
            )
        )
        session.commit()

    conflicting_replay = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(transform_permission=True),
    )
    original_replay = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(transform_permission=False),
    )

    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json() == {
        "error": {"code": "product_asset_permission_conflict", "retryable": False}
    }
    assert original_replay.status_code == 200
    assert original_replay.json()["transform_permission"] is True
    assert original_replay.json()["generation_ready"] is True


def test_legacy_asset_without_permission_history_fails_closed_without_writes(
    migrated_client: TestClient,
    migrated_api_session_factory: sessionmaker[Session],
) -> None:
    with migrated_api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.add(
            ProductAsset(
                customer_id=CUSTOMER_A_ID,
                product_id="product-001",
                photo_id="photo-001",
                identity="original_real_photo",
                confirmed=True,
                source_type="synthetic_metadata",
                quality_flags=["focus_ok", "product_visible"],
                occurred_at=datetime(2026, 8, 30, 9, tzinfo=UTC),
                content_sha256="a" * 64,
                mime_type="image/jpeg",
                width=1200,
                height=900,
            )
        )
        session.commit()

    response = migrated_client.post(
        "/v1/product-assets", headers=CUSTOMER_A, json=asset_payload()
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "product_asset_permission_history_missing",
            "retryable": False,
        }
    }
    with migrated_api_session_factory() as session:
        assert session.query(Customer).count() == 1
        assert session.query(ProductAsset).count() == 1
        assert session.query(ProductAssetPermissionEvent).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_get_product_assets_derives_permission_from_latest_event(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    created = intake(client, transform_permission=False)
    with api_session_factory() as session:
        session.add(
            ProductAssetPermissionEvent(
                customer_id=CUSTOMER_A_ID,
                product_asset_id=UUID(created["id"]),
                transform_permission=True,
                occurred_at=datetime(2026, 8, 30, 10, tzinfo=UTC),
                provenance="synthetic_permission_review",
            )
        )
        session.commit()

    response = client.get("/v1/product-assets/product-001", headers=CUSTOMER_A)

    assert response.status_code == 200
    assert response.json()["product_id"] == "product-001"
    assert len(response.json()["assets"]) == 1
    asset = response.json()["assets"][0]
    assert asset["transform_permission"] is True
    assert asset["generation_ready"] is True
    assert asset["readiness_reasons"] == []


def test_cross_customer_product_lookup_returns_404(client: TestClient) -> None:
    intake(client)

    response = client.get("/v1/product-assets/product-001", headers=CUSTOMER_B)

    assert response.status_code == 404


def test_identity_comes_only_from_customer_header(client: TestClient) -> None:
    missing_header = client.post("/v1/product-assets", json=asset_payload())
    body_identity = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(customer_id=str(CUSTOMER_B_ID)),
    )

    assert missing_header.status_code == 422
    assert body_identity.status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confirmed", "true"),
        ("source_type", "unknown_source"),
        ("content_sha256", "A" * 64),
        ("content_sha256", "a" * 63),
        ("mime_type", "IMAGE/JPEG"),
        ("mime_type", "image/gif"),
        ("width", 0),
        ("width", True),
        ("height", 1.5),
        ("occurred_at", "2026-08-30T09:00:00"),
        ("quality_flags", ["valid", ""]),
        ("quality_flags", [f"flag-{index}" for index in range(21)]),
        ("photo_id", "/srv/customer/photo.jpg"),
        ("photo_id", r"C:\\customer\\photo.jpg"),
        ("product_id", "../customer/product.json"),
    ],
)
def test_intake_rejects_malformed_metadata(
    client: TestClient, field: str, value: object
) -> None:
    response = client.post(
        "/v1/product-assets", headers=CUSTOMER_A, json=asset_payload(**{field: value})
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "unsafe_flag",
    [
        "folder/photo",
        r"folder\photo",
        "../photo",
        "https://example.test/photo",
        "focus ok",
        "focus:ok",
        "Focus_ok",
        " focus_ok",
        "focus_ok ",
    ],
)
def test_quality_flags_reject_paths_urls_whitespace_and_colons(
    client: TestClient, unsafe_flag: str
) -> None:
    response = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(quality_flags=[unsafe_flag]),
    )

    assert response.status_code == 422


def test_quality_flags_accept_only_bounded_machine_label_characters(
    client: TestClient,
) -> None:
    response = intake(
        client,
        quality_flags=["focus.ok", "focus-ok", "focus_ok", "focus2"],
    )

    assert response["quality_flags"] == [
        "focus.ok",
        "focus-ok",
        "focus_ok",
        "focus2",
    ]


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        1788070800,
        1788070800.5,
        "1788070800",
        "2026-08-30",
        "2026-08-30T09:00:00",
        " 2026-08-30T09:00:00Z ",
    ],
)
def test_occurred_at_requires_strict_rfc3339_json_string(
    client: TestClient, invalid_timestamp: object
) -> None:
    response = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(occurred_at=invalid_timestamp),
    )

    assert response.status_code == 422


@pytest.mark.parametrize("path_field", ["storage_path", "file_path", "server_path"])
def test_http_body_never_accepts_server_file_paths(
    client: TestClient, path_field: str
) -> None:
    response = client.post(
        "/v1/product-assets",
        headers=CUSTOMER_A,
        json=asset_payload(**{path_field: "/srv/private/customer.jpg"}),
    )

    assert response.status_code == 422


def test_concurrent_identical_intake_creates_one_asset_and_one_permission_event(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.commit()
    gate_concurrent_missing_asset_reads(monkeypatch)

    def create_asset() -> tuple[int, dict[str, Any]]:
        response = client.post(
            "/v1/product-assets", headers=CUSTOMER_A, json=asset_payload()
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_asset(), range(2)))

    assert sorted(status for status, _ in results) == [200, 201]
    assert len({payload["id"] for _, payload in results}) == 1
    with api_session_factory() as session:
        assert session.query(ProductAsset).count() == 1
        assert session.query(ProductAssetPermissionEvent).count() == 1
        assert session.query(AuditEvent).count() == 2


def test_concurrent_conflicting_hash_intake_keeps_one_immutable_identity(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with api_session_factory() as session:
        session.add(Customer(id=CUSTOMER_A_ID, display_name="Customer A"))
        session.commit()
    gate_concurrent_missing_asset_reads(monkeypatch)

    def create_asset(content_sha256: str) -> tuple[int, dict[str, Any]]:
        response = client.post(
            "/v1/product-assets",
            headers=CUSTOMER_A,
            json=asset_payload(content_sha256=content_sha256),
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_asset, ["a" * 64, "b" * 64]))

    assert sorted(status for status, _ in results) == [201, 409]
    conflict = next(payload for status, payload in results if status == 409)
    assert conflict == {
        "error": {"code": "product_asset_content_conflict", "retryable": False}
    }
    with api_session_factory() as session:
        assert session.query(ProductAsset).count() == 1
        assert session.query(ProductAssetPermissionEvent).count() == 1
        assert session.query(AuditEvent).count() == 2


def test_latest_permission_order_is_stable_when_events_share_business_time(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    """A second permission fact cannot reuse an asset's business timestamp."""
    created = intake(client)
    with api_session_factory() as session:
        session.add(
            ProductAssetPermissionEvent(
                customer_id=CUSTOMER_A_ID,
                product_asset_id=UUID(created["id"]),
                transform_permission=False,
                occurred_at=datetime.fromisoformat(OCCURRED_AT),
                provenance="synthetic_permission_review",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_get_missing_product_returns_404(client: TestClient) -> None:
    response = client.get("/v1/product-assets/missing-product", headers=CUSTOMER_A)

    assert response.status_code == 404


def test_later_permission_event_withdraws_generation_readiness(
    client: TestClient, api_session_factory: sessionmaker[Session]
) -> None:
    created = intake(client)
    with api_session_factory() as session:
        session.add(
            ProductAssetPermissionEvent(
                customer_id=CUSTOMER_A_ID,
                product_asset_id=UUID(created["id"]),
                transform_permission=False,
                occurred_at=datetime(2026, 8, 30, 9, tzinfo=UTC)
                + timedelta(seconds=1),
                provenance="synthetic_permission_withdrawal",
            )
        )
        session.commit()

    asset = client.get(
        "/v1/product-assets/product-001", headers=CUSTOMER_A
    ).json()["assets"][0]
    assert asset["transform_permission"] is False
    assert asset["generation_ready"] is False
    assert asset["readiness_reasons"] == ["transform_permission_denied"]
