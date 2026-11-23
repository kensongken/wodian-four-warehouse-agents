"""Customer confirmation is the only boundary that authors historical suites."""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.db import create_database_engine
from opc_style.main import create_app
from opc_style.models import (
    Base,
    Customer,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
    StyleSample,
)
from opc_style.schemas import HistoricalStyleGroupsConfirmRequest
from opc_style.services.styles import HistoricalStyleConfirmationService

A = UUID("11111111-1111-1111-1111-111111111111")
B = UUID("22222222-2222-2222-2222-222222222222")
HEADERS = {"X-Customer-Id": str(A)}
ENDPOINT = "/v1/style-cards/scenes/confirm"


@pytest.fixture
def factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'confirmation.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [Customer(id=A, display_name="A"), Customer(id=B, display_name="B")]
        )
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def client(factory: sessionmaker[Session]) -> Generator[TestClient, None, None]:
    app = create_app()

    def session_override() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    # No analyzer is installed: a confirmation must never run analysis again.
    with TestClient(app) as client:
        yield client


def evidence(
    factory: sessionmaker[Session],
    *,
    owner: UUID = A,
    state: str = "active",
    consent_provenance: str = "synthetic_opt_in",
) -> str:
    with factory() as session:
        sample = StyleSample(
            customer_id=owner,
            photo_id=str(uuid4()),
            authorized_at=datetime.now(UTC),
            consent_provenance=consent_provenance,
            consent_text_version="v1",
            content_sha256="a" * 64,
            mime_type="image/jpeg",
            width=10,
            height=12,
            asset_store_namespace="synthetic-only",
            withdrawn_at=datetime.now(UTC) if state == "withdrawn" else None,
        )
        if state == "incomplete":
            sample.consent_text_version = None
            sample.content_sha256 = None
            sample.mime_type = None
            sample.width = None
            sample.height = None
            sample.asset_store_namespace = None
        session.add(sample)
        session.commit()
        return str(sample.id)


def test_mixed_demo_confirmation_returns_a_customer_visible_conflict(
    client: TestClient,
    factory: sessionmaker[Session],
) -> None:
    """A mixed-origin confirmation must be a handled 409, never an opaque 500."""
    customer_sample = evidence(factory)
    demo_sample = evidence(factory, consent_provenance="local_synthetic_acceptance")

    response = client.post(
        ENDPOINT,
        headers=HEADERS,
        json=confirmation(
            [group("mixed-origin", [customer_sample, demo_sample], ["hero"])]
        ),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "historical_style_origin_mismatch"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0


def test_retained_demo_style_can_coexist_with_same_name_customer_upload(
    client: TestClient,
    factory: sessionmaker[Session],
) -> None:
    """Origin is part of active name identity; historical demo rows stay retained."""
    demo_sample = evidence(factory, consent_provenance="local_synthetic_acceptance")
    customer_sample = evidence(factory)
    demo_request = HistoricalStyleGroupsConfirmRequest.model_validate(
        confirmation([group("同名暖光", [demo_sample], ["hero"])])
    )
    with factory() as session:
        demo_snapshot = HistoricalStyleConfirmationService(session).confirm(
            A,
            demo_request,
            style_origin="local_demo",
        )[0]

    uploaded = client.post(
        ENDPOINT,
        headers=HEADERS,
        json=confirmation([group("同名暖光", [customer_sample], ["hero"])]),
    )

    assert uploaded.status_code == 201, uploaded.text
    with factory() as session:
        scenes = list(
            session.scalars(
                select(SceneStyle)
                .where(SceneStyle.customer_id == A, SceneStyle.active.is_(True))
                .order_by(SceneStyle.style_origin)
            )
        )
        assert [(scene.name, scene.style_origin) for scene in scenes] == [
            ("同名暖光", "customer_upload"),
            ("同名暖光", "local_demo"),
        ]
        assert session.get(SceneStyle, demo_snapshot.scene.id) is not None


def slot(key: str) -> dict[str, Any]:
    return {
        "slot_key": key,
        "label": key,
        "rules": {
            "aspect_ratio": "4:5",
            "shot_distance": "close",
            "camera_angle": "front",
            "composition": ["centered"],
            "background": ["white"],
            "lighting": ["soft"],
            "props": ["none"],
            "negative_rules": ["no text"],
        },
    }


def group(name: str, sample_ids: list[str], slots: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "evidence_sample_ids": sample_ids,
        "proposal_keys": ["visual-1"],
        "tone": ["warm"],
        "slots": [slot(key) for key in slots],
    }


def confirmation(groups: list[dict[str, Any]]) -> dict[str, Any]:
    return {"confirmation_key": str(uuid4()), "confirmed": True, "groups": groups}


def test_customer_confirms_two_historical_styles_with_different_slots(
    client: TestClient,
    factory: sessionmaker[Session],
) -> None:
    first, second = evidence(factory), evidence(factory)
    response = client.post(
        ENDPOINT,
        headers=HEADERS,
        json=confirmation(
            [
                group("warm-table", [first], ["hero", "detail", "wear"]),
                group("clean-white", [second], ["hero", "side"]),
            ]
        ),
    )
    assert response.status_code == 201, response.text
    rows = response.json()
    assert [(row["name"], [s["slot_key"] for s in row["slots"]]) for row in rows] == [
        ("warm-table", ["hero", "detail", "wear"]),
        ("clean-white", ["hero", "side"]),
    ]
    assert all(row["main_style_card_version_id"] is None for row in rows)
    assert all(row["source"] == "customer_confirmation" for row in rows)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(StyleCard)
                .where(StyleCard.kind == "main")
            )
            == 0
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM style_template_slots")
            ).scalar_one()
            == 5
        )
        links = session.execute(
            text(
                "SELECT style_card_version_id, style_sample_id FROM style_card_sample_links"
            )
        ).all()
        assert set(links) == {
            (rows[0]["version_id"], first),
            (rows[1]["version_id"], second),
        }
        assert rows[0]["slots"][0]["style_card_version_id"] == rows[0]["version_id"]


@pytest.mark.parametrize("state", ["foreign", "withdrawn", "incomplete", "missing"])
def test_invalid_evidence_rejects_entire_confirmation_before_writes(
    client: TestClient,
    factory: sessionmaker[Session],
    state: str,
) -> None:
    valid = evidence(factory)
    invalid = (
        str(uuid4())
        if state == "missing"
        else evidence(factory, owner=B if state == "foreign" else A, state=state)
    )
    response = client.post(
        ENDPOINT,
        headers=HEADERS,
        json=confirmation(
            [
                group("valid-first", [valid], ["hero"]),
                group("invalid-second", [invalid], ["hero"]),
            ]
        ),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "historical_style_evidence_unavailable"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 0
        assert session.scalar(select(func.count()).select_from(StyleCard)) == 0


@pytest.mark.parametrize(
    "change",
    [
        "unconfirmed",
        "missing_confirmation",
        "duplicate_slot",
        "empty_slots",
        "duplicate_evidence",
    ],
)
def test_confirmation_requires_explicit_valid_customer_choices(
    client: TestClient,
    factory: sessionmaker[Session],
    change: str,
) -> None:
    sample = evidence(factory)
    body = confirmation([group("history", [sample], ["hero"])])
    if change == "unconfirmed":
        body["confirmed"] = False
    elif change == "missing_confirmation":
        del body["confirmed"]
    elif change == "duplicate_slot":
        body["groups"][0]["slots"].append(slot("hero"))
    elif change == "empty_slots":
        body["groups"][0]["slots"] = []
    else:
        body["groups"][0]["evidence_sample_ids"].append(sample)
    assert client.post(ENDPOINT, headers=HEADERS, json=body).status_code == 422


def test_repeated_confirmation_reuses_original_versions_and_conflicting_key_is_rejected(
    client: TestClient,
    factory: sessionmaker[Session],
) -> None:
    body = confirmation([group("history", [evidence(factory)], ["hero"])])
    first = client.post(ENDPOINT, headers=HEADERS, json=body)
    assert first.status_code == 201, first.text
    repeated = client.post(ENDPOINT, headers=HEADERS, json=body)
    assert repeated.json() == first.json()
    body["groups"][0]["name"] = "changed"
    conflict = client.post(ENDPOINT, headers=HEADERS, json=body)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "style_confirmation_conflict"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 1


def test_reconfirmation_of_same_named_style_appends_version_instead_of_conflict(
    client: TestClient,
    factory: sessionmaker[Session],
) -> None:
    first_sample = evidence(factory)
    second_sample = evidence(factory)
    first = client.post(
        ENDPOINT,
        headers=HEADERS,
        json=confirmation([group("history", [first_sample], ["hero"])]),
    )
    assert first.status_code == 201, first.text

    refreshed = client.post(
        ENDPOINT,
        headers=HEADERS,
        json=confirmation(
            [group("history", [first_sample, second_sample], ["hero", "detail"])]
        ),
    )

    assert refreshed.status_code == 201, refreshed.text
    assert refreshed.json()[0]["id"] == first.json()[0]["id"]
    assert refreshed.json()[0]["version"] == 2
    assert refreshed.json()[0]["sample_count"] == 2
    assert [slot["slot_key"] for slot in refreshed.json()[0]["slots"]] == [
        "hero",
        "detail",
    ]
    assert refreshed.json()[0]["source"] == "customer_reconfirmation"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SceneStyle)) == 1
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 2


def test_edit_and_deactivation_preserve_readable_original_slots_and_evidence(
    client: TestClient,
    factory: sessionmaker[Session],
) -> None:
    body = confirmation([group("history", [evidence(factory)], ["hero", "detail"])])
    result = client.post(ENDPOINT, headers=HEADERS, json=body)
    assert result.status_code == 201, result.text
    original = result.json()[0]
    scene_url = f"/v1/style-cards/scenes/{original['id']}"
    edited = client.patch(
        scene_url,
        headers=HEADERS,
        json={"name": "new name", "slots": [slot("hero"), slot("wear")]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2
    assert [s["slot_key"] for s in edited.json()["slots"]] == ["hero", "wear"]
    assert edited.json()["slots"][0]["id"] != original["slots"][0]["id"]
    assert client.post(scene_url + "/deactivate", headers=HEADERS).status_code == 200
    history = client.get(
        scene_url + f"/versions/{original['version_id']}", headers=HEADERS
    )
    assert history.status_code == 200, history.text
    assert history.json()["slots"] == original["slots"]
    assert history.json()["name"] == "history"
    assert (
        client.get(
            scene_url + f"/versions/{original['version_id']}",
            headers={"X-Customer-Id": str(B)},
        ).status_code
        == 404
    )
    exported = client.get("/v1/style-export", headers=HEADERS).json()[
        "scene_style_cards"
    ][0]["versions"]
    assert [s["slot_key"] for s in exported[0]["slots"]] == ["hero", "detail"]
    assert [s["slot_key"] for s in exported[1]["slots"]] == ["hero", "wear"]
    assert exported[0]["evidence_sample_ids"] == exported[1]["evidence_sample_ids"]
    assert (
        client.get("/v1/style-cards/scenes", headers=HEADERS).json()[0]["status"]
        == "inactive"
    )


def test_clearing_historical_channel_rules_keeps_revision_and_list_readable(
    client: TestClient,
    factory: sessionmaker[Session],
) -> None:
    created = client.post(
        ENDPOINT,
        headers=HEADERS,
        json=confirmation([group("history", [evidence(factory)], ["hero"])]),
    )
    assert created.status_code == 201, created.text
    scene_url = f"/v1/style-cards/scenes/{created.json()[0]['id']}"
    configured = client.patch(
        scene_url,
        headers=HEADERS,
        json={
            "usage_rules": ["keep jewelry detail"],
            "aspect_ratio": "4:5",
            "purpose": "catalog",
        },
    )
    assert configured.status_code == 200, configured.text

    cleared = client.patch(
        scene_url,
        headers=HEADERS,
        json={"usage_rules": None, "aspect_ratio": None, "purpose": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["usage_rules"] == []
    assert cleared.json()["aspect_ratio"] is None
    assert cleared.json()["purpose"] is None
    listing = client.get("/v1/style-cards/scenes", headers=HEADERS)
    assert listing.status_code == 200, listing.text
    assert listing.json()[0]["usage_rules"] == []
    exact = client.get(
        scene_url + f"/versions/{cleared.json()['version_id']}", headers=HEADERS
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["usage_rules"] == []
    assert exact.json()["slots"] == cleared.json()["slots"]
    previous = client.get(
        scene_url + f"/versions/{configured.json()['version_id']}", headers=HEADERS
    )
    assert previous.status_code == 200, previous.text
    assert previous.json()["usage_rules"] == ["keep jewelry detail"]
    with factory() as session:
        version = session.get(StyleCardVersion, UUID(cleared.json()["version_id"]))
        assert version is not None
        assert not {"usage_rules", "aspect_ratio", "purpose"}.intersection(
            version.content
        )


def test_withdrawal_after_confirmation_blocks_new_revision(
    client: TestClient,
    factory: sessionmaker[Session],
) -> None:
    sample = evidence(factory)
    first = client.post(
        ENDPOINT,
        headers=HEADERS,
        json=confirmation([group("history", [sample], ["hero"])]),
    )
    assert first.status_code == 201, first.text
    with factory() as session:
        row = session.get(StyleSample, UUID(sample))
        assert row is not None
        row.withdrawn_at = datetime.now(UTC)
        session.commit()
    result = client.patch(
        f"/v1/style-cards/scenes/{first.json()[0]['id']}",
        headers=HEADERS,
        json={"name": "new"},
    )
    assert result.status_code == 409
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(StyleCardVersion)) == 1


def test_migrated_slot_history_is_immutable_and_version_binding_is_customer_scoped(
    migrated_session: Session,
) -> None:
    # Exercise the real migration and service, including direct SQL protection.
    from opc_style.schemas import HistoricalStyleGroupsConfirmRequest
    from opc_style.services.styles import HistoricalStyleConfirmationService

    session = migrated_session
    session.add_all(
        [Customer(id=A, display_name="A"), Customer(id=B, display_name="B")]
    )
    session.commit()
    sample = StyleSample(
        customer_id=A,
        photo_id="synthetic",
        authorized_at=datetime.now(UTC),
        consent_provenance="opt_in",
        consent_text_version="v1",
        content_sha256="a" * 64,
        mime_type="image/jpeg",
        width=10,
        height=12,
        asset_store_namespace="synthetic",
    )
    session.add(sample)
    session.commit()
    request = HistoricalStyleGroupsConfirmRequest.model_validate(
        confirmation([group("history", [str(sample.id)], ["hero"])])
    )
    snapshots = HistoricalStyleConfirmationService(session).confirm(A, request)
    slot_id = snapshots[0].version.content["slots"][0]["id"]
    for statement in (
        "UPDATE style_template_slots SET label='changed' WHERE id=:id",
        "DELETE FROM style_template_slots WHERE id=:id",
    ):
        with pytest.raises(IntegrityError, match="style_template_slot_immutable"):
            session.execute(text(statement), {"id": slot_id})
        session.rollback()
    columns = "id,customer_id,scene_style_id,style_card_version_id,slot_key,label,rules,active,position,created_at"
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                f"INSERT INTO style_template_slots ({columns}) SELECT :id,:customer,scene_style_id,style_card_version_id,'other',label,rules,active,position,created_at FROM style_template_slots WHERE id=:old"
            ),
            {"id": str(uuid4()), "customer": str(B), "old": slot_id},
        )
    session.rollback()
    with pytest.raises(IntegrityError, match="style_template_slot_snapshot_mismatch"):
        session.execute(
            text(
                f"INSERT INTO style_template_slots ({columns}) SELECT :id,customer_id,scene_style_id,style_card_version_id,'extra',label,rules,active,position+1,created_at FROM style_template_slots WHERE id=:old"
            ),
            {"id": str(uuid4()), "old": slot_id},
        )
    session.rollback()
