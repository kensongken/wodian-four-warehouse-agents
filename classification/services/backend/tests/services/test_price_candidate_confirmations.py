import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from product_engine.services.conservative_pricing import (
    PriceAnchor,
    build_conservative_price_candidate,
)
from product_engine.services.price_candidate_confirmations import (
    PriceCandidateConfirmationFailure,
    confirm_conservative_price_candidate,
)
from product_engine.services.product_passport_drafts import build_product_passport_draft
from product_engine.services.training_label_versions import (
    ServiceRuleInput,
    TrainingLabelInput,
    TrainingLabelVersionStore,
)


def human_values() -> TrainingLabelInput:
    return TrainingLabelInput(
        sha256="c" * 64,
        main_image_template_code="LOCAL-GREEN-BRACELET",
        product_type="first_sale_bead",
        inventory_status="in_stock",
        material_label="绿松",
        product_form="手串",
        visual_observations=["绿色调明显"],
        selling_points=["近景实拍展示珠体轮廓"],
        specification_note="珠径 5 mm；尺寸由训练员测量",
        quality_note="图片可见颜色分布较均匀，仅为观察记录",
        defect_note="图片可见轻微表面纹理",
        price_range=None,
        service_rules=[
            ServiceRuleInput(
                service_code="scan_identity",
                eligibility="购买后可用",
                customer_summary="购买后可扫码进入身份与服务页面",
            )
        ],
    )


def price_anchor() -> PriceAnchor:
    return PriceAnchor(
        standard_code="PRICE-TURQUOISE-BRACELET-001",
        category_code="turquoise",
        form="手串",
        specification_summary="珠径约 5 mm",
        quality_summary="颜色分布较均匀",
        defect_summary="允许轻微表面纹理",
        minimum=Decimal("1200"),
        maximum=Decimal("1500"),
        basis="训练员录入的同品类同形态价格标准",
        anchor_version=1,
        confirmation_source="trainer_confirmed",
        confirmed_by="synthetic-trainer-001",
        confirmed_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )


def prepared_store(tmp_path):
    store = TrainingLabelVersionStore(
        tmp_path / "versions.jsonl",
        tmp_path / "initial.jsonl",
    )
    initial = store.register_initial("LOCAL-GREEN-BRACELET", human_values())
    current = store.save("LOCAL-GREEN-BRACELET", initial.version, human_values())
    return store, current


def test_human_confirmation_appends_new_version_with_price_audit(tmp_path) -> None:
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

    assert saved.version == current.version + 1
    assert saved.previous_version_id == current.version_id
    assert saved.change_source == "trainer_confirmed"
    assert saved.price_range is not None
    assert saved.price_range.minimum == "1200.00"
    assert saved.price_range.maximum == "1500.00"
    assert saved.price_confirmation is not None
    assert saved.price_confirmation.standard_code == anchor.standard_code
    assert saved.price_confirmation.anchor_version == 1
    assert saved.price_confirmation.anchor_confirmed_by == "synthetic-trainer-001"
    assert saved.price_confirmation.anchor_confirmed_at == anchor.confirmed_at
    assert saved.price_confirmation.confirmed_by == "synthetic-trainer-002"
    assert saved.price_confirmation.confirmed_at == confirmed_at
    assert saved.price_confirmation.automatic_publication_allowed is False


def test_package_preflight_failure_does_not_append_confirmed_version(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    draft = build_product_passport_draft(current)
    anchor = price_anchor()
    candidate = build_conservative_price_candidate(draft, anchor)
    before = store.version_path.read_bytes()

    def reject_package(_saved):
        raise RuntimeError("synthetic_package_failure")

    with pytest.raises(RuntimeError, match="synthetic_package_failure"):
        confirm_conservative_price_candidate(
            store=store,
            draft=draft,
            price_candidate=candidate,
            price_anchor=anchor,
            base_version=current.version,
            confirmed_by="synthetic-trainer-002",
            before_append=reject_package,
        )

    assert store.version_path.read_bytes() == before
    assert store.current(current.template_code) == current


def test_stale_or_blank_confirmation_does_not_append(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    draft = build_product_passport_draft(current)
    anchor = price_anchor()
    candidate = build_conservative_price_candidate(draft, anchor)
    before = store.version_path.read_bytes()

    with pytest.raises(PriceCandidateConfirmationFailure, match="version_conflict"):
        confirm_conservative_price_candidate(
            store=store,
            draft=draft,
            price_candidate=candidate,
            price_anchor=anchor,
            base_version=current.version - 1,
            confirmed_by="synthetic-trainer-002",
        )
    with pytest.raises(PriceCandidateConfirmationFailure, match="confirmed_by_required"):
        confirm_conservative_price_candidate(
            store=store,
            draft=draft,
            price_candidate=candidate,
            price_anchor=anchor,
            base_version=current.version,
            confirmed_by="   ",
        )

    assert store.version_path.read_bytes() == before


def test_mutated_price_candidate_is_not_saved(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    draft = build_product_passport_draft(current)
    anchor = price_anchor()
    candidate = build_conservative_price_candidate(draft, anchor).model_copy(
        update={"minimum": Decimal("1300")}
    )
    before = store.version_path.read_bytes()

    with pytest.raises(
        PriceCandidateConfirmationFailure,
        match="invalid_conservative_price_candidate",
    ):
        confirm_conservative_price_candidate(
            store=store,
            draft=draft,
            price_candidate=candidate,
            price_anchor=anchor,
            base_version=current.version,
            confirmed_by="synthetic-trainer-002",
        )

    assert store.version_path.read_bytes() == before


def test_confirmation_rejects_draft_that_differs_from_current_version(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    draft = build_product_passport_draft(current)
    mutated_draft = draft.model_copy(update={"specification_note": "被改写的规格"})
    anchor = price_anchor()
    candidate = build_conservative_price_candidate(draft, anchor)
    before = store.version_path.read_bytes()

    with pytest.raises(PriceCandidateConfirmationFailure, match="current_draft_mismatch"):
        confirm_conservative_price_candidate(
            store=store,
            draft=mutated_draft,
            price_candidate=candidate,
            price_anchor=anchor,
            base_version=current.version,
            confirmed_by="synthetic-trainer-002",
        )

    assert store.version_path.read_bytes() == before


def test_rollback_restores_price_and_its_confirmation_audit_together(tmp_path) -> None:
    store, current = prepared_store(tmp_path)
    draft = build_product_passport_draft(current)
    anchor = price_anchor()
    candidate = build_conservative_price_candidate(draft, anchor)
    confirmed = confirm_conservative_price_candidate(
        store=store,
        draft=draft,
        price_candidate=candidate,
        price_anchor=anchor,
        base_version=current.version,
        confirmed_by="synthetic-trainer-002",
    )
    later = store.save(
        confirmed.template_code,
        confirmed.version,
        TrainingLabelInput(**confirmed.model_dump()).model_copy(
            update={"selling_points": ["人工调整后的候选卖点"]}
        ),
    )

    restored = store.rollback(later.template_code, later.version)

    assert restored.price_range == confirmed.price_range
    assert restored.price_confirmation == confirmed.price_confirmation


def test_store_reads_legacy_price_audit_without_anchor_confirmation_fields(tmp_path) -> None:
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
    records = [
        json.loads(line) for line in store.version_path.read_text(encoding="utf-8").splitlines()
    ]
    legacy_audit = records[-1]["price_confirmation"]
    legacy_audit.pop("anchor_confirmed_by")
    legacy_audit.pop("anchor_confirmed_at")
    store.version_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    restored = store.current(confirmed.template_code)

    assert restored is not None
    assert restored.price_confirmation is not None
    assert restored.price_confirmation.standard_code == anchor.standard_code
    assert restored.price_confirmation.anchor_confirmed_by is None
    assert restored.price_confirmation.anchor_confirmed_at is None
