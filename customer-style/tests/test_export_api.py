"""Contract tests for customer-owned style exports."""

from collections.abc import Generator
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from opc_style.db import create_database_engine
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    Base,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
    StyleSample,
)
from opc_style.services.exports import _timestamp
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}


@pytest.fixture
def api_session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'export-api.db'}")
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
    with LocalSyntheticAssetStore(tmp_path / "export-synthetic-content") as store:
        app.state.classification_bridge_store = store
        with TestClient(app) as test_client:
            yield test_client


def authorize_content(
    client: TestClient, headers: dict[str, str], photo_id: str
) -> Response:
    image = BytesIO()
    Image.new("RGB", (10, 12), "white").save(image, format="JPEG")
    content = image.getvalue()
    return client.post(
        "/v1/style-samples/content",
        headers={
            **headers,
            "X-Photo-Id": photo_id,
            "X-Content-Sha256": sha256(content).hexdigest(),
            "X-Mime-Type": "image/jpeg",
            "X-Consent-Granted": "true",
            "X-Consent-Provenance": "synthetic-export-consent",
            "X-Consent-Text-Version": "consent-v1",
        },
        content=content,
    )


@pytest.fixture
def seeded_two_customers(client: TestClient) -> None:
    for headers, photo_id in (
        (CUSTOMER_A, "customer-a-photo"),
        (CUSTOMER_B, "customer-b-photo"),
    ):
        response = authorize_content(client, headers, photo_id)
        assert response.status_code == 201


def test_export_contains_only_request_customer(
    client: TestClient, seeded_two_customers: None
) -> None:
    response = client.get("/v1/style-export", headers=CUSTOMER_A)

    assert response.status_code == 200
    assert CUSTOMER_B["X-Customer-Id"] not in response.text
    assert response.json()["schema"] == "opc.style-export.v1"


def test_export_has_stable_safe_history_for_all_main_and_scene_versions(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    seeded_two_customers: None,
) -> None:
    samples = []
    for number in range(10):
        response = authorize_content(
            client, CUSTOMER_A, f"customer-a-evidence-{number:02d}"
        )
        assert response.status_code == 201
        samples.append(response.json())

    generated = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)
    assert generated.status_code == 201
    main_card_id = generated.json()["id"]
    edited_main = client.patch(
        "/v1/style-cards/main", headers=CUSTOMER_A, json={"tone": ["清透"]}
    )
    assert edited_main.status_code == 200
    created_scene = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={
            "name": "展柜",
            "lighting": ["珠宝柜台光"],
            "aspect_ratio": "4:5",
            "purpose": "商城展柜主图",
            "usage_rules": ["保留珠宝细节"],
        },
    )
    assert created_scene.status_code == 201
    scene_id = created_scene.json()["id"]
    edited_scene = client.patch(
        f"/v1/style-cards/scenes/{scene_id}",
        headers=CUSTOMER_A,
        json={
            "name": "展柜近景",
            "aspect_ratio": "9:16",
            "purpose": "竖版展柜近景",
            "usage_rules": ["仅用于竖版内容", "保留珠宝细节"],
        },
    )
    assert edited_scene.status_code == 200
    deactivated_scene = client.post(
        f"/v1/style-cards/scenes/{scene_id}/deactivate", headers=CUSTOMER_A
    )
    assert deactivated_scene.status_code == 200
    withdrawn = client.post(
        f"/v1/style-samples/{samples[0]['id']}/withdraw", headers=CUSTOMER_A
    )
    assert withdrawn.status_code == 200

    with api_session_factory() as session:
        session.add(
            AuditEvent(
                customer_id=CUSTOMER_A_ID,
                event_type="style_export.safety_probe",
                payload={
                    "style_card_id": main_card_id,
                    "database_path": "/private/style.db",
                    "media_path": "/private/photo.jpg",
                    "environment": "production-secret",
                    "token": "do-not-export",
                    "other_customer_id": str(CUSTOMER_B_ID),
                },
            )
        )
        session.commit()

    first_response = client.get("/v1/style-export", headers=CUSTOMER_A)
    second_response = client.get("/v1/style-export", headers=CUSTOMER_A)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.content == second_response.content
    export = first_response.json()
    assert export["customer_id"] == str(CUSTOMER_A_ID)
    assert export["samples"][1]["status"] == "withdrawn"
    assert [entry["action"] for entry in export["samples"][1]["consent_history"]] == [
        "authorized",
        "withdrawn",
    ]
    assert [
        version["version"] for version in export["main_style_cards"][0]["versions"]
    ] == [
        1,
        2,
    ]
    assert export["main_style_cards"][0]["versions"][0]["content"]["analyzer"] == (
        "deterministic-local-v1"
    )
    assert (
        len(export["main_style_cards"][0]["versions"][0]["evidence_sample_ids"]) == 11
    )
    assert export["scene_style_cards"][0]["scene"] == {
        "id": scene_id,
        "name": "展柜近景",
        "active": False,
    }
    assert [
        version["version"] for version in export["scene_style_cards"][0]["versions"]
    ] == [
        1,
        2,
    ]
    first_scene_content = export["scene_style_cards"][0]["versions"][0]["content"]
    second_scene_content = export["scene_style_cards"][0]["versions"][1]["content"]
    assert first_scene_content["aspect_ratio"] == "4:5"
    assert first_scene_content["purpose"] == "商城展柜主图"
    assert first_scene_content["usage_rules"] == ["保留珠宝细节"]
    assert second_scene_content["aspect_ratio"] == "9:16"
    assert second_scene_content["purpose"] == "竖版展柜近景"
    assert second_scene_content["usage_rules"] == [
        "仅用于竖版内容",
        "保留珠宝细节",
    ]
    probe = next(
        action
        for action in export["audit_actions"]
        if action["action"] == "style_export.safety_probe"
    )
    assert probe["details"] == {"style_card_id": main_card_id}
    assert str(CUSTOMER_B_ID) not in first_response.text
    for secret in (
        "/private/style.db",
        "/private/photo.jpg",
        "production-secret",
        "do-not-export",
    ):
        assert secret not in first_response.text


def test_export_drops_foreign_references_from_allowlisted_json_fields(
    client: TestClient,
    api_session_factory: sessionmaker[Session],
    seeded_two_customers: None,
) -> None:
    authorized_samples = []
    for number in range(9):
        response = authorize_content(
            client, CUSTOMER_A, f"customer-a-reference-{number:02d}"
        )
        assert response.status_code == 201
        authorized_samples.append(response.json())

    generated = client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A)
    assert generated.status_code == 201
    main_card_id = generated.json()["id"]
    main_version_id = generated.json()["version_id"]
    created_scene = client.post(
        "/v1/style-cards/scenes",
        headers=CUSTOMER_A,
        json={"name": "可信场景"},
    )
    assert created_scene.status_code == 201
    scene_id = created_scene.json()["id"]

    with api_session_factory() as session:
        customer_b_sample = session.scalar(
            select(StyleSample).where(StyleSample.customer_id == CUSTOMER_B_ID)
        )
        assert customer_b_sample is not None
        customer_b_card = StyleCard(customer_id=CUSTOMER_B_ID, kind="main")
        session.add(customer_b_card)
        session.flush()
        customer_b_version = StyleCardVersion(
            customer_id=CUSTOMER_B_ID,
            style_card_id=customer_b_card.id,
            version=1,
            content={"source": "customer-b"},
        )
        session.add(customer_b_version)
        customer_b_scene = SceneStyle(
            customer_id=CUSTOMER_B_ID,
            style_card_id=customer_b_card.id,
            name="customer-b-scene",
        )
        session.add_all([customer_b_version, customer_b_scene])
        session.flush()
        session.add_all(
            [
                StyleCardVersion(
                    customer_id=CUSTOMER_A_ID,
                    style_card_id=UUID(main_card_id),
                    version=2,
                    content={
                        "source": "malicious-main-content",
                        "evidence_photo_ids": [
                            "customer-a-reference-00",
                            customer_b_sample.photo_id,
                        ],
                    },
                ),
                StyleCardVersion(
                    customer_id=CUSTOMER_A_ID,
                    style_card_id=UUID(scene_id),
                    version=2,
                    content={
                        "source": "malicious-scene-content",
                        "main_style_card_version_id": str(customer_b_version.id),
                        "overrides": {},
                    },
                ),
            ]
        )
        session.add_all(
            [
                AuditEvent(
                    customer_id=CUSTOMER_A_ID,
                    event_type="style_export.legitimate_refs",
                    payload={
                        "sample_id": authorized_samples[0]["id"],
                        "photo_id": "customer-a-reference-00",
                        "style_card_id": main_card_id,
                        "style_card_version_id": main_version_id,
                        "main_style_card_version_id": main_version_id,
                        "scene_id": scene_id,
                        "version_id": main_version_id,
                    },
                ),
                AuditEvent(
                    customer_id=CUSTOMER_A_ID,
                    event_type="style_export.foreign_refs",
                    payload={
                        "sample_id": str(customer_b_sample.id),
                        "photo_id": customer_b_sample.photo_id,
                        "style_card_id": str(customer_b_card.id),
                        "style_card_version_id": str(customer_b_version.id),
                        "main_style_card_version_id": str(customer_b_version.id),
                        "scene_id": str(customer_b_scene.id),
                        "version_id": str(customer_b_version.id),
                    },
                ),
            ]
        )
        session.commit()

    response = client.get("/v1/style-export", headers=CUSTOMER_A)

    assert response.status_code == 200
    actions = {
        action["action"]: action["details"]
        for action in response.json()["audit_actions"]
    }
    assert actions["style_export.legitimate_refs"] == {
        "main_style_card_version_id": main_version_id,
        "photo_id": "customer-a-reference-00",
        "sample_id": authorized_samples[0]["id"],
        "scene_id": scene_id,
        "style_card_id": main_card_id,
        "style_card_version_id": main_version_id,
        "version_id": main_version_id,
    }
    assert actions["style_export.foreign_refs"] == {}
    assert str(CUSTOMER_B_ID) not in response.text
    export = response.json()
    assert export["main_style_cards"][0]["versions"][1]["content"][
        "evidence_photo_ids"
    ] == ["customer-a-reference-00"]
    assert (
        "main_style_card_version_id"
        not in export["scene_style_cards"][0]["versions"][1]["content"]
    )


def test_export_timestamp_normalizes_naive_and_aware_values_to_utc() -> None:
    aware_utc_time = datetime(2026, 8, 30, 12, 34, 56, 123456, tzinfo=UTC)
    naive_sqlite_time = aware_utc_time.replace(tzinfo=None)

    assert _timestamp(naive_sqlite_time) == "2026-08-30T12:34:56.123456Z"
    assert _timestamp(aware_utc_time) == "2026-08-30T12:34:56.123456Z"
