from collections.abc import Callable
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.models import SceneStyle, StyleCard, StyleSample
from opc_style.repositories import StyleRepository

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")


def test_repository_hides_another_customers_sample(
    session: Session, sample_factory: Callable[..., StyleSample]
) -> None:
    sample = sample_factory(customer_id=CUSTOMER_A_ID)

    assert StyleRepository(session).get_sample(CUSTOMER_B_ID, sample.id) is None


def test_repository_lists_only_the_requesting_customers_samples(
    session: Session, sample_factory: Callable[..., StyleSample]
) -> None:
    own_sample = sample_factory(customer_id=CUSTOMER_A_ID)
    sample_factory(customer_id=CUSTOMER_B_ID)

    samples = StyleRepository(session).list_samples(CUSTOMER_A_ID)

    assert [sample.id for sample in samples] == [own_sample.id]


def test_repository_hides_another_customers_scene(
    session: Session, scene_factory: Callable[..., SceneStyle]
) -> None:
    scene = scene_factory(customer_id=CUSTOMER_A_ID, name="Storefront")

    assert StyleRepository(session).get_scene_style(CUSTOMER_B_ID, scene.id) is None


def test_customer_cannot_have_two_active_main_style_cards(
    session: Session, customer_factory: Callable[[UUID], object]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    session.add(StyleCard(customer_id=CUSTOMER_A_ID, kind="main", active=True))
    session.flush()
    session.add(StyleCard(customer_id=CUSTOMER_A_ID, kind="main", active=True))

    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()
