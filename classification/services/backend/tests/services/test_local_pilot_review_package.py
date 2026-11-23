import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

import product_engine.services.local_pilot_review_package as review_packages
from product_engine.services.conservative_pricing import build_conservative_price_candidate
from product_engine.services.local_pilot_review_package import (
    LocalPilotReviewPackageFailure,
    build_local_pilot_review_package,
    verify_local_pilot_review_package,
)
from product_engine.services.price_candidate_confirmations import (
    confirm_conservative_price_candidate,
)
from product_engine.services.product_passport_drafts import build_product_passport_draft

from .test_price_candidate_confirmations import prepared_store, price_anchor


def test_green_version_builds_stable_tamper_evident_review_package(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    draft = build_product_passport_draft(current)
    anchor = price_anchor()
    saved = confirm_conservative_price_candidate(
        store=store,
        draft=draft,
        price_candidate=build_conservative_price_candidate(draft, anchor),
        price_anchor=anchor,
        base_version=current.version,
        confirmed_by="synthetic-trainer-002",
        confirmed_at=datetime(2026, 8, 30, 14, 0, tzinfo=UTC),
    )

    first = build_local_pilot_review_package(saved)
    second = build_local_pilot_review_package(saved)

    assert first == second
    assert first.contract_version == "0.1-local-pilot-review"
    assert first.template_code == "LOCAL-GREEN-BRACELET"
    assert first.source_version == saved.version
    assert first.decision.decision == "allowed_for_local_pilot"
    assert first.content_package.source_version == saved.version
    assert first.local_synthetic_only is True
    assert first.real_customer_data_allowed is False
    assert first.external_actions_allowed is False
    assert first.publication_allowed is False
    assert len(first.package_sha256) == 64
    assert verify_local_pilot_review_package(first) is True

    tampered = first.model_copy(update={"template_code": "CHANGED"})
    assert verify_local_pilot_review_package(tampered) is False


def test_yellow_version_cannot_build_review_package(tmp_path) -> None:
    _, current = prepared_store(tmp_path)

    with pytest.raises(LocalPilotReviewPackageFailure, match="local_pilot_not_allowed"):
        build_local_pilot_review_package(current)


def test_untouched_current_package_has_green_inspection(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
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
    package = build_local_pilot_review_package(saved)

    inspection = review_packages.inspect_local_pilot_review_package(package, saved, saved)

    assert inspection.model_dump(mode="json") == {
        "signal": "green",
        "code": "verified_current",
        "title_zh": "封条有效，且是最新版",
        "summary_zh": "审阅包内容未被改动，版本与当前商品资料一致。",
        "template_code": "LOCAL-GREEN-BRACELET",
        "package_version": saved.version,
        "current_version": saved.version,
        "usable_for_local_review": True,
        "external_actions_allowed": False,
    }


def test_untouched_old_package_has_yellow_inspection(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
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
    package = build_local_pilot_review_package(saved)
    newer = saved.model_copy(update={"version": saved.version + 1})

    inspection = review_packages.inspect_local_pilot_review_package(package, saved, newer)

    assert inspection.signal == "yellow"
    assert inspection.code == "verified_stale"
    assert inspection.current_version == saved.version + 1
    assert inspection.usable_for_local_review is False


def test_tampered_package_has_red_inspection(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
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
    package = build_local_pilot_review_package(saved)
    tampered = package.model_copy(update={"source_version": saved.version + 20})

    inspection = review_packages.inspect_local_pilot_review_package(tampered, saved, saved)

    assert inspection.signal == "red"
    assert inspection.code == "package_tampered"
    assert inspection.usable_for_local_review is False


def test_valid_package_without_current_product_is_yellow(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
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
    package = build_local_pilot_review_package(saved)

    inspection = review_packages.inspect_local_pilot_review_package(package, saved, None)

    assert inspection.signal == "yellow"
    assert inspection.code == "current_product_not_found"
    assert inspection.current_version is None
    assert inspection.usable_for_local_review is False


def test_package_identity_must_match_nested_decision_and_content_package(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
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
    payload = build_local_pilot_review_package(saved).model_dump(mode="json")
    payload["decision"]["template_code"] = "OTHER-PRODUCT"

    with pytest.raises(ValidationError, match="package_identity_mismatch"):
        review_packages.LocalPilotReviewPackage.model_validate(payload)


def test_recomputed_public_hash_cannot_forge_authoritative_package(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
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
    payload = build_local_pilot_review_package(saved).model_dump(mode="json")
    payload["content_package"]["draft"]["quality_note"] = "伪造的品质结论"
    canonical = json.dumps(
        {key: value for key, value in payload.items() if key != "package_sha256"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload["package_sha256"] = hashlib.sha256(canonical).hexdigest()
    forged = review_packages.LocalPilotReviewPackage.model_validate(payload)

    inspection = review_packages.inspect_local_pilot_review_package(
        forged,
        saved,
        saved,
    )

    assert inspection.signal == "red"
    assert inspection.code == "package_not_authoritative"


def test_current_record_must_still_pass_pilot_gate_for_green(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
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
    package = build_local_pilot_review_package(saved)
    incomplete_current = saved.model_copy(update={"inventory_status": None})

    inspection = review_packages.inspect_local_pilot_review_package(
        package,
        saved,
        incomplete_current,
    )

    assert inspection.signal == "yellow"
    assert inspection.code == "current_not_eligible"


def test_current_dangerous_claim_is_red_even_for_authoritative_package(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
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
    package = build_local_pilot_review_package(saved)
    dangerous_current = saved.model_copy(update={"selling_points": ["官方认证正品并保证升值"]})

    inspection = review_packages.inspect_local_pilot_review_package(
        package,
        saved,
        dangerous_current,
    )

    assert inspection.signal == "red"
    assert inspection.code == "current_product_blocked"
