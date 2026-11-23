"""Immutable reference packs bind real customer style evidence to generation."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    Customer,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
)

A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _service_type():
    try:
        from opc_style.services.style_reference_packs import StyleReferencePackService
    except ModuleNotFoundError:
        pytest.fail("StyleReferencePackService is not implemented")
    return StyleReferencePackService


def _confirmed_version(
    session: Session,
    *,
    customer_id: UUID = A,
    sample_count: int = 4,
) -> tuple[StyleCardVersion, list[StyleSample]]:
    session.add(Customer(id=customer_id, display_name=f"Customer {customer_id}"))
    card = StyleCard(customer_id=customer_id, kind="scene")
    session.add(card)
    session.flush()
    version = StyleCardVersion(
        customer_id=customer_id,
        style_card_id=card.id,
        version=1,
        content={
            "historical_style": True,
            "stability": "customer_confirmed",
            "style_origin": "customer_upload",
            "reference_selection_version": "pixel-diversity-farthest-v1",
        },
    )
    session.add(version)
    session.flush()
    samples: list[StyleSample] = []
    for index in range(sample_count):
        sample = StyleSample(
            customer_id=customer_id,
            photo_id=f"real-style-{customer_id}-{index}",
            authorized_at=datetime.now(UTC),
            consent_provenance="local-explicit-style-upload",
            consent_text_version="local-style-upload-v1",
            content_sha256=f"{index + 1:064x}",
            mime_type="image/jpeg",
            width=100,
            height=100,
            asset_store_namespace="real-style-test-store",
        )
        session.add(sample)
        session.flush()
        session.add(
            StyleCardSampleLink(
                customer_id=customer_id,
                style_card_version_id=version.id,
                style_sample_id=sample.id,
            )
        )
        samples.append(sample)
    session.commit()
    return version, samples


def test_pack_seals_two_to_four_customer_owned_samples(session: Session) -> None:
    """Removing the pack count or tenant check must break this test."""
    version, samples = _confirmed_version(session, sample_count=6)

    pack = _service_type()(session).get_or_create(A, version.id)

    assert pack.customer_id == A
    assert pack.scene_style_version_id == version.id
    assert pack.selection_version == "pixel-diversity-farthest-v1"
    assert pack.confirmed_by == str(A)
    assert pack.active is True
    assert len(pack.references) == 4
    assert [row.sample_id for row in pack.references] == [
        sample.id for sample in samples[:4]
    ]
    assert len(pack.manifest_sha256) == 64


def test_exact_pack_request_reuses_the_same_immutable_pack(session: Session) -> None:
    """Removing idempotent load-by-manifest must create a second pack and fail."""
    version, _ = _confirmed_version(session)
    service = _service_type()(session)

    first = service.get_or_create(A, version.id)
    replay = service.get_or_create(A, version.id)

    assert replay == first


def test_pack_rejects_cross_customer_and_insufficient_evidence(session: Session) -> None:
    """Weakening ownership or the two-reference floor must break this test."""
    version, _ = _confirmed_version(session, sample_count=1)
    session.add(Customer(id=B, display_name="Customer B"))
    session.commit()

    with pytest.raises(DomainRuleError, match="style_reference_pack_unavailable"):
        _service_type()(session).get_or_create(B, version.id)
    with pytest.raises(DomainRuleError, match="style_reference_pack_unavailable"):
        _service_type()(session).get_or_create(A, version.id)


def test_pack_read_fails_after_reference_permission_is_withdrawn(
    session: Session,
) -> None:
    """Skipping current permission revalidation must break this test."""
    version, samples = _confirmed_version(session)
    service = _service_type()(session)
    created = service.get_or_create(A, version.id)
    samples[0].withdrawn_at = datetime.now(UTC)
    session.commit()

    with pytest.raises(DomainRuleError, match="style_reference_pack_unavailable"):
        service.get(A, created.id)


def test_demo_samples_cannot_enter_a_real_reference_pack(session: Session) -> None:
    """Allowing synthetic demo provenance into a paid pack must break this test."""
    version, samples = _confirmed_version(session)
    samples[0].consent_provenance = "local_synthetic_acceptance"
    session.commit()

    with pytest.raises(DomainRuleError, match="style_reference_pack_unavailable"):
        _service_type()(session).get_or_create(A, version.id)
