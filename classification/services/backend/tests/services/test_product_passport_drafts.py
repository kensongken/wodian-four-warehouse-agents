from datetime import UTC, datetime
from uuid import uuid4

import pytest

from product_engine.services.product_passport_drafts import (
    ProductPassportDraftFailure,
    build_product_passport_draft,
)
from product_engine.services.training_label_versions import (
    PriceRangeInput,
    ServiceRuleInput,
    TrainingLabelVersion,
)


def training_version(**updates) -> TrainingLabelVersion:
    values = {
        "sha256": "a" * 64,
        "main_image_template_code": "LOCAL-GREEN-BRACELET",
        "material_label": "绿松",
        "product_form": "手串",
        "visual_observations": ["绿色调明显", "珠体排列成环"],
        "selling_points": ["近景实拍展示珠体轮廓"],
        "specification_note": "",
        "quality_note": "",
        "defect_note": "",
        "price_range": None,
        "version_id": uuid4(),
        "template_code": "LOCAL-GREEN-BRACELET",
        "version": 3,
        "previous_version_id": uuid4(),
        "rollback_target_version_id": None,
        "change_source": "trainer_confirmed",
        "material_evidence": "trainer_confirmed",
        "authenticity_verification": "not_verified",
        "origin_verification": "not_verified",
        "quality_verification": "not_verified",
        "warehouse_verification": "pending_warehouse_verification",
        "training_state": "label_confirmed_model_not_trained",
        "created_at": datetime(2026, 8, 17, 8, 44, tzinfo=UTC),
        "can_rollback": True,
    }
    values.update(updates)
    return TrainingLabelVersion.model_validate(values)


def test_confirmed_label_builds_blocked_draft_with_machine_readable_gaps() -> None:
    draft = build_product_passport_draft(training_version())

    assert draft.template_code == "LOCAL-GREEN-BRACELET"
    assert draft.source_version == 3
    assert draft.approved_attributes.material == "绿松"
    assert draft.approved_attributes.form == "手串"
    assert draft.approved_attributes.selling_points == ["近景实拍展示珠体轮廓"]
    assert draft.material_classification.category_code == "turquoise"
    assert draft.material_classification.category_label == "绿松石"
    assert draft.material_classification.decision_status == "human_confirmed"
    assert draft.material_classification.review_required is False
    assert draft.material_classification.mall_import_allowed is True
    assert draft.material_classification.evidence_refs[0].reference_id.startswith(
        "LOCAL-GREEN-BRACELET:"
    )
    assert [blocker.code for blocker in draft.blockers] == [
        "product_type_missing",
        "specification_missing",
        "quality_defect_review_missing",
        "price_range_missing",
        "inventory_missing",
        "service_rules_missing",
    ]
    assert draft.publication_status == "blocked"
    assert draft.mall_import_allowed is False
    assert draft.sales_journey_usage == "preview_only"
    assert draft.price_advisory is None
    serialized = draft.model_dump(mode="json")
    assert "product_id" not in serialized
    assert "product_passport_id" not in serialized
    assert "availability_status" not in serialized


def test_out_of_scope_human_label_stays_information_insufficient_and_blocks_handoff() -> None:
    draft = build_product_passport_draft(training_version(material_label="蜜蜡"))

    assert draft.material_classification.category_code == "information_insufficient"
    assert draft.material_classification.decision_status == "information_insufficient"
    assert draft.material_classification.review_required is True
    assert draft.material_classification.mall_import_allowed is False
    assert draft.blockers[0].code == "material_classification_missing"
    assert draft.mall_import_allowed is False


def test_draft_id_is_stable_for_same_template_and_version() -> None:
    version = training_version()

    first = build_product_passport_draft(version)
    second = build_product_passport_draft(version)

    assert first.draft_id == second.draft_id


def test_completed_human_fields_remove_only_their_blockers() -> None:
    version = training_version(
        specification_note="珠径约 5mm，由训练员测量",
        quality_note="表面光泽明显，仅为可见观察",
        price_range=PriceRangeInput(
            minimum="1200",
            maximum="1500",
            basis="训练员录入的同品类建议销售区间",
        ),
    )

    draft = build_product_passport_draft(version)

    assert [blocker.code for blocker in draft.blockers] == [
        "product_type_missing",
        "inventory_missing",
        "service_rules_missing",
    ]
    assert draft.price_advisory is not None
    assert draft.price_advisory.minimum == "1200"
    assert draft.price_advisory.maximum == "1500"
    assert draft.publication_status == "blocked"


def test_human_service_rule_is_exposed_and_removes_service_rule_blocker() -> None:
    rule = ServiceRuleInput(
        service_code="scan_identity",
        eligibility="购买后可用",
        customer_summary="购买后可扫码进入身份与服务页面",
    )

    draft = build_product_passport_draft(training_version(service_rules=[rule]))

    assert draft.service_rules == [rule]
    assert "service_rules_missing" not in {blocker.code for blocker in draft.blockers}
    assert draft.publication_status == "blocked"
    assert draft.mall_import_allowed is False


@pytest.mark.parametrize(
    "product_type",
    ["first_sale_bead", "legacy_material", "redesign_output"],
)
def test_human_product_type_is_exposed_and_removes_missing_blocker(
    product_type,
) -> None:
    draft = build_product_passport_draft(training_version(product_type=product_type))

    assert draft.product_type == product_type
    assert "product_type_missing" not in {blocker.code for blocker in draft.blockers}
    assert draft.publication_status == "blocked"
    assert draft.mall_import_allowed is False


@pytest.mark.parametrize(
    "inventory_status",
    ["in_stock", "reserved", "out_of_stock"],
)
def test_human_inventory_status_is_exposed_and_removes_inventory_blocker(
    inventory_status,
) -> None:
    draft = build_product_passport_draft(training_version(inventory_status=inventory_status))

    assert draft.inventory_status == inventory_status
    assert "inventory_missing" not in {blocker.code for blocker in draft.blockers}
    assert draft.publication_status == "blocked"
    assert draft.mall_import_allowed is False
    assert "availability_status" not in draft.model_dump(mode="json")


def test_exact_match_history_cannot_become_handoff_draft() -> None:
    version = training_version(
        change_source="exact_match_reused",
        material_evidence="merchant_declared",
        match_method="exact_sha256",
        matched_from_template_code="LOCAL-SOURCE",
    )

    with pytest.raises(
        ProductPassportDraftFailure,
        match="human_confirmation_required",
    ):
        build_product_passport_draft(version)
