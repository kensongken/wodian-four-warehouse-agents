from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy.orm import Session

from alembic import command
from opc_style.db import create_database_engine
from opc_style.models import (
    Base,
    Customer,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
    StyleSample,
)

WORKTREE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def migrated_session(tmp_path: Path) -> Generator[Session, None, None]:
    database_url = f"sqlite:///{tmp_path / 'migrated-style.db'}"
    config = Config(str(WORKTREE_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(WORKTREE_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


@pytest.fixture
def customer_factory(session: Session) -> Callable[[UUID], Customer]:
    def create_customer(customer_id: UUID) -> Customer:
        customer = session.get(Customer, customer_id)
        if customer is None:
            customer = Customer(id=customer_id, display_name=f"Customer {customer_id}")
            session.add(customer)
            session.flush()
        return customer

    return create_customer


@pytest.fixture
def sample_factory(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> Callable[..., StyleSample]:
    def create_sample(customer_id: UUID, photo_id: str | None = None) -> StyleSample:
        customer_factory(customer_id)
        sample = StyleSample(
            id=uuid4(),
            customer_id=customer_id,
            photo_id=photo_id or f"photo-{uuid4()}",
            authorized_at=datetime.now(UTC),
            consent_provenance="synthetic-test-consent",
            consent_text_version="synthetic-test-consent-v1",
            content_sha256="a" * 64,
            mime_type="image/jpeg",
            width=1,
            height=1,
            asset_store_namespace="synthetic-test-store",
        )
        session.add(sample)
        session.flush()
        return sample

    return create_sample


@pytest.fixture
def style_card_factory(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> Callable[..., StyleCard]:
    def create_style_card(customer_id: UUID, kind: str = "derived") -> StyleCard:
        customer_factory(customer_id)
        card = StyleCard(id=uuid4(), customer_id=customer_id, kind=kind)
        session.add(card)
        session.flush()
        return card

    return create_style_card


@pytest.fixture
def style_card_version_factory(
    session: Session, style_card_factory: Callable[..., StyleCard]
) -> Callable[..., StyleCardVersion]:
    def create_version(
        customer_id: UUID, style_card: StyleCard | None = None
    ) -> StyleCardVersion:
        card = style_card or style_card_factory(customer_id=customer_id)
        version = StyleCardVersion(
            id=uuid4(),
            customer_id=customer_id,
            style_card_id=card.id,
            version=1,
            content={"tone": "quiet"},
        )
        session.add(version)
        session.flush()
        return version

    return create_version


@pytest.fixture
def scene_factory(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> Callable[..., SceneStyle]:
    def create_scene(customer_id: UUID, name: str, active: bool = True) -> SceneStyle:
        customer_factory(customer_id)
        scene = SceneStyle(
            id=uuid4(), customer_id=customer_id, name=name, active=active
        )
        session.add(scene)
        session.flush()
        return scene

    return create_scene
