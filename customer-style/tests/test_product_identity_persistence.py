"""ORM persistence contracts for product identity analysis and card history."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    ProductAsset,
    ProductIdentityAnalysisRun,
    ProductIdentityCardVersion,
)

CUSTOMER_A = UUID("00000000-0000-0000-0000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-0000-0000-0000000000b2")
NOW = datetime(2026, 9, 12, tzinfo=UTC)


@pytest.fixture
def product_asset(session: Session, customer_factory) -> ProductAsset:
    customer_factory(CUSTOMER_A)
    asset = ProductAsset(
        customer_id=CUSTOMER_A,
        product_id="product-a",
        photo_id="photo-a",
        identity="original_real_photo",
        confirmed=True,
        source_type="classification_handoff",
        quality_flags=[],
        occurred_at=NOW,
        content_sha256="a" * 64,
        mime_type="image/png",
        width=10,
        height=10,
        storage_path="asset.png",
    )
    session.add(asset)
    session.flush()
    return asset


@pytest.fixture
def other_product_asset(session: Session, product_asset: ProductAsset) -> ProductAsset:
    asset = ProductAsset(
        customer_id=CUSTOMER_A,
        product_id="product-b",
        photo_id="photo-b",
        identity="original_real_photo",
        confirmed=True,
        source_type="classification_handoff",
        quality_flags=[],
        occurred_at=NOW,
        content_sha256="e" * 64,
        mime_type="image/png",
        width=10,
        height=10,
        storage_path="asset-b.png",
    )
    session.add(asset)
    session.flush()
    return asset


@pytest.fixture
def analysis_run(
    session: Session, product_asset: ProductAsset
) -> ProductIdentityAnalysisRun:
    run = ProductIdentityAnalysisRun(
        customer_id=CUSTOMER_A,
        product_asset_id=product_asset.id,
        product_id=product_asset.product_id,
        source_content_sha256=product_asset.content_sha256,
        idempotency_key="identity-1",
        model="controlled-model",
        authorized_usd=Decimal("0.50"),
        input_manifest_sha256="b" * 64,
        input_manifest={"asset_id": str(product_asset.id), "image_count": 1},
        dispatch_state="reserved",
        outbound_attempts=0,
    )
    session.add(run)
    session.flush()
    return run


@pytest.fixture
def confirmed_card(
    session: Session,
    product_asset: ProductAsset,
    analysis_run: ProductIdentityAnalysisRun,
) -> ProductIdentityCardVersion:
    card = ProductIdentityCardVersion(
        customer_id=CUSTOMER_A,
        product_asset_id=product_asset.id,
        product_id=product_asset.product_id,
        analysis_run_id=analysis_run.id,
        version=1,
        source_content_sha256=product_asset.content_sha256,
        card_payload={"category": {"certainty": "observed", "value": "bracelet"}},
        card_sha256="c" * 64,
    )
    session.add(card)
    session.flush()
    return card


def test_analysis_run_allows_only_outcome_progress(
    session: Session, analysis_run: ProductIdentityAnalysisRun
) -> None:
    analysis_run.dispatch_state = "provider_responded"
    analysis_run.outbound_attempts = 1
    analysis_run.proposal = {"category": {"certainty": "unclear", "value": None}}
    analysis_run.failure_code = None
    session.flush()

    analysis_run.input_manifest["image_count"] = 2
    flag_modified(analysis_run, "input_manifest")
    with pytest.raises(
        DomainRuleError, match="product_identity_analysis_authorization_immutable"
    ):
        session.flush()


def test_analysis_run_rejects_cross_customer_asset_product_scope(
    session: Session, customer_factory, product_asset: ProductAsset
) -> None:
    customer_factory(CUSTOMER_B)
    run = ProductIdentityAnalysisRun(
        customer_id=CUSTOMER_B,
        product_asset_id=product_asset.id,
        product_id=product_asset.product_id,
        source_content_sha256=product_asset.content_sha256,
        idempotency_key="wrong-tenant",
        model="controlled-model",
        authorized_usd=Decimal("0.50"),
        input_manifest_sha256="b" * 64,
        input_manifest={},
    )
    session.add(run)
    with pytest.raises(IntegrityError):
        session.flush()


def test_confirmed_identity_card_scope_and_payload_are_immutable(
    session: Session, confirmed_card: ProductIdentityCardVersion
) -> None:
    assert confirmed_card.confirmed_by == "local_customer"
    confirmed_card.card_payload["category"]["value"] = "necklace"
    flag_modified(confirmed_card, "card_payload")
    with pytest.raises(DomainRuleError, match="product_identity_card_immutable"):
        session.flush()


def test_confirmed_identity_card_cannot_be_deleted(
    session: Session, confirmed_card: ProductIdentityCardVersion
) -> None:
    session.delete(confirmed_card)
    with pytest.raises(DomainRuleError, match="product_identity_card_immutable"):
        session.flush()


def test_identity_card_rejects_duplicate_asset_version(
    session: Session,
    product_asset: ProductAsset,
    analysis_run: ProductIdentityAnalysisRun,
    confirmed_card: ProductIdentityCardVersion,
) -> None:
    duplicate = ProductIdentityCardVersion(
        customer_id=CUSTOMER_A,
        product_asset_id=product_asset.id,
        product_id=product_asset.product_id,
        analysis_run_id=analysis_run.id,
        version=confirmed_card.version,
        source_content_sha256=product_asset.content_sha256,
        card_payload={"category": {"certainty": "unclear", "value": None}},
        card_sha256="d" * 64,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("mismatch", ["asset_and_product", "source_digest"])
def test_identity_card_must_match_its_analysis_run_lineage(
    session: Session,
    product_asset: ProductAsset,
    other_product_asset: ProductAsset,
    analysis_run: ProductIdentityAnalysisRun,
    mismatch: str,
) -> None:
    card = ProductIdentityCardVersion(
        customer_id=CUSTOMER_A,
        product_asset_id=(
            other_product_asset.id
            if mismatch == "asset_and_product"
            else product_asset.id
        ),
        product_id=(
            other_product_asset.product_id
            if mismatch == "asset_and_product"
            else product_asset.product_id
        ),
        analysis_run_id=analysis_run.id,
        version=1,
        source_content_sha256=other_product_asset.content_sha256,
        card_payload={"category": {"certainty": "observed", "value": "bracelet"}},
        card_sha256="f" * 64,
    )
    session.add(card)

    with pytest.raises(IntegrityError):
        session.flush()
