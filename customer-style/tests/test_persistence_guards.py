from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    AuditEvent,
    Customer,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
)
from opc_style.repositories import StyleRepository

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")


def test_sqlite_connections_enable_foreign_key_enforcement(session: Session) -> None:
    assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_sample_requires_explicit_authorization_and_consent_provenance(
    session: Session, customer_factory: Callable[[UUID], object]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    session.add(
        StyleSample(
            id=uuid4(),
            customer_id=CUSTOMER_A_ID,
            photo_id="missing-explicit-consent",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_repository_creates_samples_only_with_explicit_consent(
    session: Session, customer_factory: Callable[[UUID], object]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    repository = StyleRepository(session)

    with pytest.raises(ValueError, match="consent_provenance"):
        repository.create_sample(
            CUSTOMER_A_ID,
            "empty-consent",
            authorized_at=datetime.now(UTC),
            consent_provenance=" ",
        )

    sample = repository.create_sample(
        CUSTOMER_A_ID,
        "explicit-consent",
        authorized_at=datetime(2026, 8, 30, tzinfo=UTC),
        consent_provenance="customer-confirmed-style-sample",
    )
    session.flush()

    assert sample.authorized_at == datetime(2026, 8, 30, tzinfo=UTC)
    assert sample.consent_provenance == "customer-confirmed-style-sample"


def test_version_rejects_a_style_card_owned_by_another_customer(
    session: Session, style_card_factory: Callable[..., StyleCard]
) -> None:
    card = style_card_factory(customer_id=CUSTOMER_A_ID)
    session.add(
        StyleCardVersion(
            id=uuid4(),
            customer_id=CUSTOMER_B_ID,
            style_card_id=card.id,
            version=1,
            content={"tone": "wrong-owner"},
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_sample_link_rejects_a_sample_owned_by_another_customer(
    session: Session,
    sample_factory: Callable[..., StyleSample],
    style_card_version_factory: Callable[..., StyleCardVersion],
) -> None:
    version = style_card_version_factory(customer_id=CUSTOMER_B_ID)
    sample = sample_factory(customer_id=CUSTOMER_A_ID)
    session.add(
        StyleCardSampleLink(
            id=uuid4(),
            customer_id=CUSTOMER_B_ID,
            style_card_version_id=version.id,
            style_sample_id=sample.id,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_scene_rejects_a_style_card_owned_by_another_customer(
    session: Session, style_card_factory: Callable[..., StyleCard]
) -> None:
    card = style_card_factory(customer_id=CUSTOMER_A_ID)
    session.add(
        SceneStyle(
            id=uuid4(),
            customer_id=CUSTOMER_B_ID,
            style_card_id=card.id,
            name="Wrong owner",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_style_card_version_rejects_updates(
    session: Session, style_card_version_factory: Callable[..., StyleCardVersion]
) -> None:
    version = style_card_version_factory(customer_id=CUSTOMER_A_ID)
    version.content = {"tone": "mutated"}

    with pytest.raises(DomainRuleError, match="style_card_version_immutable"):
        session.flush()


def test_style_card_version_rejects_deletes(
    session: Session, style_card_version_factory: Callable[..., StyleCardVersion]
) -> None:
    version = style_card_version_factory(customer_id=CUSTOMER_A_ID)
    session.delete(version)

    with pytest.raises(DomainRuleError, match="style_card_version_immutable"):
        session.flush()


def test_style_card_sample_link_rejects_updates(
    session: Session,
    sample_factory: Callable[..., StyleSample],
    style_card_version_factory: Callable[..., StyleCardVersion],
) -> None:
    version = style_card_version_factory(customer_id=CUSTOMER_A_ID)
    original_sample = sample_factory(customer_id=CUSTOMER_A_ID)
    replacement_sample = sample_factory(customer_id=CUSTOMER_A_ID)
    link = StyleCardSampleLink(
        customer_id=CUSTOMER_A_ID,
        style_card_version_id=version.id,
        style_sample_id=original_sample.id,
    )
    session.add(link)
    session.flush()
    link.style_sample_id = replacement_sample.id

    with pytest.raises(DomainRuleError, match="style_card_sample_link_immutable"):
        session.flush()


def test_style_card_sample_link_rejects_deletes(
    session: Session,
    sample_factory: Callable[..., StyleSample],
    style_card_version_factory: Callable[..., StyleCardVersion],
) -> None:
    version = style_card_version_factory(customer_id=CUSTOMER_A_ID)
    sample = sample_factory(customer_id=CUSTOMER_A_ID)
    link = StyleCardSampleLink(
        customer_id=CUSTOMER_A_ID,
        style_card_version_id=version.id,
        style_sample_id=sample.id,
    )
    session.add(link)
    session.flush()
    session.delete(link)

    with pytest.raises(DomainRuleError, match="style_card_sample_link_immutable"):
        session.flush()


def test_audit_event_rejects_updates(session: Session) -> None:
    session.add(Customer(id=CUSTOMER_A_ID, display_name="A"))
    session.flush()
    audit_event = AuditEvent(
        customer_id=CUSTOMER_A_ID,
        event_type="style_sample.authorized",
        payload={"sample_id": "sample-001"},
    )
    session.add(audit_event)
    session.flush()
    audit_event.event_type = "style_sample.withdrawn"

    with pytest.raises(DomainRuleError, match="audit_event_immutable"):
        session.flush()


def test_audit_event_rejects_deletes(session: Session) -> None:
    session.add(Customer(id=CUSTOMER_A_ID, display_name="A"))
    session.flush()
    audit_event = AuditEvent(
        customer_id=CUSTOMER_A_ID,
        event_type="style_sample.authorized",
        payload={"sample_id": "sample-001"},
    )
    session.add(audit_event)
    session.flush()
    session.delete(audit_event)

    with pytest.raises(DomainRuleError, match="audit_event_immutable"):
        session.flush()


def test_repository_rejects_the_sixth_active_scene(session: Session) -> None:
    session.add(Customer(id=CUSTOMER_A_ID, display_name="A"))
    session.flush()
    repository = StyleRepository(session)
    for number in range(5):
        repository.create_scene_style(
            CUSTOMER_A_ID, name=f"Scene {number}", active=True
        )

    with pytest.raises(DomainRuleError, match="scene_limit_reached"):
        repository.create_scene_style(CUSTOMER_A_ID, name="Scene 5", active=True)


def test_metadata_schema_rejects_cross_customer_card_links(session: Session) -> None:
    session.execute(
        text(
            "INSERT INTO customers (id, display_name, created_at) VALUES "
            "(:id, :name, :created_at)"
        ),
        [
            {
                "id": str(CUSTOMER_A_ID),
                "name": "A",
                "created_at": datetime.now(UTC).isoformat(),
            },
            {
                "id": str(CUSTOMER_B_ID),
                "name": "B",
                "created_at": datetime.now(UTC).isoformat(),
            },
        ],
    )
    card_id = uuid4()
    session.execute(
        text(
            "INSERT INTO style_cards (id, customer_id, kind, active, created_at) "
            "VALUES (:id, :customer_id, 'derived', 1, :created_at)"
        ),
        {
            "id": str(card_id),
            "customer_id": str(CUSTOMER_A_ID),
            "created_at": datetime.now(UTC).isoformat(),
        },
    )

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO style_card_versions "
                "(id, customer_id, style_card_id, version, content, created_at) "
                "VALUES (:id, :customer_id, :style_card_id, 1, '{}', :created_at)"
            ),
            {
                "id": str(uuid4()),
                "customer_id": str(CUSTOMER_B_ID),
                "style_card_id": str(card_id),
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

    session.rollback()


def test_alembic_migration_enforces_ownership_and_immutable_versions(
    migrated_session: Session,
) -> None:
    assert migrated_session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    created_at = datetime.now(UTC).isoformat()
    migrated_session.execute(
        text(
            "INSERT INTO customers (id, display_name, created_at) VALUES "
            "(:id, :name, :created_at)"
        ),
        [
            {"id": str(CUSTOMER_A_ID), "name": "A", "created_at": created_at},
            {"id": str(CUSTOMER_B_ID), "name": "B", "created_at": created_at},
        ],
    )
    card_id = uuid4()
    migrated_session.execute(
        text(
            "INSERT INTO style_cards (id, customer_id, kind, active, created_at) "
            "VALUES (:id, :customer_id, 'derived', 1, :created_at)"
        ),
        {"id": str(card_id), "customer_id": str(CUSTOMER_A_ID), "created_at": created_at},
    )
    migrated_session.commit()

    with pytest.raises(IntegrityError):
        migrated_session.execute(
            text(
                "INSERT INTO style_card_versions "
                "(id, customer_id, style_card_id, version, content, created_at) "
                "VALUES (:id, :customer_id, :style_card_id, 1, '{}', :created_at)"
            ),
            {
                "id": str(uuid4()),
                "customer_id": str(CUSTOMER_B_ID),
                "style_card_id": str(card_id),
                "created_at": created_at,
            },
        )
    migrated_session.rollback()

    version_id = uuid4()
    migrated_session.execute(
        text(
            "INSERT INTO style_card_versions "
            "(id, customer_id, style_card_id, version, content, created_at) "
            "VALUES (:id, :customer_id, :style_card_id, 1, '{}', :created_at)"
        ),
        {
            "id": str(version_id),
            "customer_id": str(CUSTOMER_A_ID),
            "style_card_id": str(card_id),
            "created_at": created_at,
        },
    )
    migrated_session.commit()

    with pytest.raises(IntegrityError, match="style_card_version_immutable"):
        migrated_session.execute(
            text("UPDATE style_card_versions SET content = '{}' WHERE id = :id"),
            {"id": str(version_id)},
        )
    migrated_session.rollback()
    with pytest.raises(IntegrityError, match="style_card_version_immutable"):
        migrated_session.execute(
            text("DELETE FROM style_card_versions WHERE id = :id"),
            {"id": str(version_id)},
        )
    migrated_session.rollback()


def test_alembic_migration_rejects_raw_audit_event_updates_and_deletes(
    migrated_session: Session,
) -> None:
    created_at = datetime.now(UTC).isoformat()
    audit_event_id = uuid4()
    migrated_session.execute(
        text(
            "INSERT INTO customers (id, display_name, created_at) VALUES "
            "(:id, :name, :created_at)"
        ),
        {"id": str(CUSTOMER_A_ID), "name": "A", "created_at": created_at},
    )
    migrated_session.execute(
        text(
            "INSERT INTO audit_events "
            "(id, customer_id, event_type, payload, occurred_at) "
            "VALUES (:id, :customer_id, :event_type, '{}', :occurred_at)"
        ),
        {
            "id": str(audit_event_id),
            "customer_id": str(CUSTOMER_A_ID),
            "event_type": "style_sample.authorized",
            "occurred_at": created_at,
        },
    )
    migrated_session.commit()

    with pytest.raises(IntegrityError, match="audit_event_immutable"):
        migrated_session.execute(
            text("UPDATE audit_events SET event_type = 'mutated' WHERE id = :id"),
            {"id": str(audit_event_id)},
        )
    migrated_session.rollback()

    with pytest.raises(IntegrityError, match="audit_event_immutable"):
        migrated_session.execute(
            text("DELETE FROM audit_events WHERE id = :id"), {"id": str(audit_event_id)}
        )
    migrated_session.rollback()


def test_alembic_migration_rejects_raw_style_card_sample_link_changes(
    migrated_session: Session,
) -> None:
    created_at = datetime.now(UTC).isoformat()
    card_id = uuid4()
    version_id = uuid4()
    sample_id = uuid4()
    replacement_sample_id = uuid4()
    link_id = uuid4()
    migrated_session.execute(
        text(
            "INSERT INTO customers (id, display_name, created_at) VALUES "
            "(:id, 'A', :created_at)"
        ),
        {"id": str(CUSTOMER_A_ID), "created_at": created_at},
    )
    migrated_session.execute(
        text(
            "INSERT INTO style_cards (id, customer_id, kind, active, created_at) "
            "VALUES (:id, :customer_id, 'main', 1, :created_at)"
        ),
        {
            "id": str(card_id),
            "customer_id": str(CUSTOMER_A_ID),
            "created_at": created_at,
        },
    )
    migrated_session.execute(
        text(
            "INSERT INTO style_card_versions "
            "(id, customer_id, style_card_id, version, content, created_at) "
            "VALUES (:id, :customer_id, :card_id, 1, '{}', :created_at)"
        ),
        {
            "id": str(version_id),
            "customer_id": str(CUSTOMER_A_ID),
            "card_id": str(card_id),
            "created_at": created_at,
        },
    )
    for current_sample_id, photo_id in (
        (sample_id, "link-original"),
        (replacement_sample_id, "link-replacement"),
    ):
        migrated_session.execute(
            text(
                "INSERT INTO style_samples "
                "(id, customer_id, photo_id, authorized_at, consent_provenance, "
                "withdrawn_at, created_at) VALUES "
                "(:id, :customer_id, :photo_id, :authorized_at, 'test', NULL, "
                ":created_at)"
            ),
            {
                "id": str(current_sample_id),
                "customer_id": str(CUSTOMER_A_ID),
                "photo_id": photo_id,
                "authorized_at": created_at,
                "created_at": created_at,
            },
        )
    migrated_session.execute(
        text(
            "INSERT INTO style_card_sample_links "
            "(id, customer_id, style_card_version_id, style_sample_id, created_at) "
            "VALUES (:id, :customer_id, :version_id, :sample_id, :created_at)"
        ),
        {
            "id": str(link_id),
            "customer_id": str(CUSTOMER_A_ID),
            "version_id": str(version_id),
            "sample_id": str(sample_id),
            "created_at": created_at,
        },
    )
    migrated_session.commit()

    with pytest.raises(IntegrityError, match="style_card_sample_link_immutable"):
        migrated_session.execute(
            text(
                "UPDATE style_card_sample_links SET style_sample_id = :sample_id "
                "WHERE id = :id"
            ),
            {"sample_id": str(replacement_sample_id), "id": str(link_id)},
        )
    migrated_session.rollback()

    with pytest.raises(IntegrityError, match="style_card_sample_link_immutable"):
        migrated_session.execute(
            text("DELETE FROM style_card_sample_links WHERE id = :id"),
            {"id": str(link_id)},
        )
    migrated_session.rollback()
