from datetime import UTC, datetime

import pytest

from product_engine.services.confirmed_product_content_package import (
    ConfirmedProductContentPackageFailure,
    build_confirmed_product_content_package,
)
from product_engine.services.conservative_pricing import build_conservative_price_candidate
from product_engine.services.price_candidate_confirmations import (
    confirm_conservative_price_candidate,
)
from product_engine.services.product_passport_drafts import build_product_passport_draft
from product_engine.services.training_label_versions import PriceRangeInput, TrainingLabelInput

from .test_price_candidate_confirmations import human_values, prepared_store, price_anchor


def test_confirmed_version_builds_audited_internal_package(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    draft = build_product_passport_draft(current)
    anchor = price_anchor()
    candidate = build_conservative_price_candidate(draft, anchor)
    confirmed_at = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)
    saved = confirm_conservative_price_candidate(
        store=store,
        draft=draft,
        price_candidate=candidate,
        price_anchor=anchor,
        base_version=current.version,
        confirmed_by="synthetic-trainer-002",
        confirmed_at=confirmed_at,
    )

    package = build_confirmed_product_content_package(saved)

    assert package.contract_version == "0.1-internal-confirmed"
    assert package.template_code == "LOCAL-GREEN-BRACELET"
    assert package.source_version == saved.version
    assert package.price_review_status == "human_confirmed"
    assert package.price_confirmation.confirmed_by == "synthetic-trainer-002"
    assert package.price_confirmation.confirmed_at == confirmed_at
    assert package.draft.price_advisory is not None
    assert package.draft.price_advisory.minimum == "1200.00"
    assert package.draft.price_advisory.maximum == "1500.00"
    assert "price_range_missing" not in [item.code for item in package.draft.blockers]
    price_claim = next(
        claim for claim in package.content_evidence.claims if claim.field_path == "price_advisory"
    )
    assert price_claim.evidence_refs == [
        f"training-label:LOCAL-GREEN-BRACELET:v{saved.version}",
        "price-anchor:PRICE-TURQUOISE-BRACELET-001:v1",
        f"price-confirmation:LOCAL-GREEN-BRACELET:v{saved.version}",
    ]
    assert package.publication_status == "blocked"
    assert package.sales_journey_usage == "preview_only"
    assert package.automatic_publication_allowed is False


def test_price_without_confirmation_audit_cannot_build_confirmed_package(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    values = human_values().model_copy(
        update={
            "price_range": PriceRangeInput(
                minimum="1200",
                maximum="1500",
                basis="训练员录入的同品类同形态价格标准",
            )
        }
    )
    saved = store.save(current.template_code, current.version, values)

    with pytest.raises(
        ConfirmedProductContentPackageFailure,
        match="price_confirmation_required",
    ):
        build_confirmed_product_content_package(saved)


def test_non_price_blockers_remain_after_price_confirmation(tmp_path) -> None:
    store, initial_current = prepared_store(tmp_path)
    incomplete_values = human_values().model_copy(
        update={"inventory_status": None, "service_rules": []}
    )
    current = store.save(
        initial_current.template_code,
        initial_current.version,
        incomplete_values,
    )
    draft = build_product_passport_draft(current)
    anchor = price_anchor()
    saved = confirm_conservative_price_candidate(
        store=store,
        draft=draft,
        price_candidate=build_conservative_price_candidate(draft, anchor),
        price_anchor=anchor,
        base_version=current.version,
        confirmed_by="synthetic-trainer-002",
    )

    package = build_confirmed_product_content_package(saved)

    assert {item.code for item in package.draft.blockers} == {
        "inventory_missing",
        "service_rules_missing",
    }


def test_rollback_version_cannot_claim_to_be_confirmation_version(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    draft = build_product_passport_draft(current)
    anchor = price_anchor()
    confirmed = confirm_conservative_price_candidate(
        store=store,
        draft=draft,
        price_candidate=build_conservative_price_candidate(draft, anchor),
        price_anchor=anchor,
        base_version=current.version,
        confirmed_by="synthetic-trainer-002",
    )
    edited = store.save(
        confirmed.template_code,
        confirmed.version,
        TrainingLabelInput(**confirmed.model_dump()),
    )
    restored = store.rollback(edited.template_code, edited.version)

    with pytest.raises(
        ConfirmedProductContentPackageFailure,
        match="price_confirmation_version_required",
    ):
        build_confirmed_product_content_package(restored)
