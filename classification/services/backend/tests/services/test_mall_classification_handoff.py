from datetime import UTC, datetime
from uuid import uuid4

import pytest

from product_engine.services.mall_classification_handoff import (
    MallClassificationHandoffFailure,
    build_mall_classification_handoff,
)
from product_engine.services.product_passport_drafts import build_product_passport_draft
from product_engine.services.training_label_versions import (
    PriceRangeInput,
    ServiceRuleInput,
    TrainingLabelVersion,
)


def training_version(material_label: str = "绿松") -> TrainingLabelVersion:
    return TrainingLabelVersion.model_validate(
        {
            "sha256": "a" * 64,
            "main_image_template_code": "LOCAL-GREEN-BRACELET",
            "material_label": material_label,
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
    )


def test_confirmed_classification_builds_mall_read_model_but_not_publishable_product() -> None:
    draft = build_product_passport_draft(training_version())

    handoff = build_mall_classification_handoff(draft)

    assert handoff.contract_version == "0.1"
    assert handoff.template_code == "LOCAL-GREEN-BRACELET"
    assert handoff.source_version == 3
    assert handoff.material_classification.category_code == "turquoise"
    assert handoff.material_classification.category_label == "绿松石"
    assert handoff.material_classification.decision_status == "human_confirmed"
    assert handoff.classification_ready_for_mall is True
    assert handoff.sales_readiness_review == "rejected"
    assert handoff.review_scope == "product_data_completeness_only"
    assert handoff.product_publication_allowed is False
    assert handoff.remaining_blockers == [
        "product_type_missing",
        "specification_missing",
        "quality_defect_review_missing",
        "price_range_missing",
        "inventory_missing",
        "service_rules_missing",
    ]
    assert handoff.verification_boundaries.authenticity == "not_verified"
    assert handoff.verification_boundaries.origin == "not_verified"
    assert handoff.verification_boundaries.quality == "not_verified"
    assert handoff.verification_boundaries.warehouse == "pending_warehouse_verification"


def test_complete_human_data_passes_readiness_review_without_authorizing_publication() -> None:
    record = training_version().model_copy(
        update={
            "product_type": "first_sale_bead",
            "inventory_status": "in_stock",
            "specification_note": "珠径 5 mm；尺寸由训练员测量",
            "quality_note": "图片可见颜色分布较均匀，仅为观察记录",
            "defect_note": "图片可见轻微表面纹理",
            "price_range": PriceRangeInput(
                minimum="1200.00",
                maximum="1500.00",
                basis="训练员录入的同品类价格标准",
            ),
            "service_rules": [
                ServiceRuleInput(
                    service_code="scan_identity",
                    eligibility="购买后可用",
                    customer_summary="购买后可扫码进入身份与服务页面",
                )
            ],
        }
    )
    draft = build_product_passport_draft(record)

    handoff = build_mall_classification_handoff(draft)

    assert handoff.remaining_blockers == []
    assert handoff.sales_readiness_review == "passed"
    assert handoff.review_scope == "product_data_completeness_only"
    assert handoff.product_publication_allowed is False
    assert handoff.verification_boundaries.authenticity == "not_verified"
    assert handoff.verification_boundaries.origin == "not_verified"
    assert handoff.verification_boundaries.quality == "not_verified"
    assert handoff.verification_boundaries.warehouse == "pending_warehouse_verification"


def test_information_insufficient_fails_closed() -> None:
    draft = build_product_passport_draft(training_version("蜜蜡"))

    with pytest.raises(
        MallClassificationHandoffFailure,
        match="classification_not_human_confirmed",
    ):
        build_mall_classification_handoff(draft)
