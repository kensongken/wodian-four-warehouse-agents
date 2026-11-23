from datetime import UTC, datetime
from uuid import uuid4

from product_engine.services.pilot_release_gate import evaluate_pilot_release
from product_engine.services.training_label_versions import (
    PriceConfirmationAudit,
    PriceRangeInput,
    ServiceRuleInput,
    TrainingLabelVersion,
)


def training_version(**updates: object) -> TrainingLabelVersion:
    base = TrainingLabelVersion.model_validate(
        {
            "sha256": "a" * 64,
            "main_image_template_code": "LOCAL-GREEN-BRACELET",
            "product_type": "first_sale_bead",
            "inventory_status": "in_stock",
            "material_label": "绿松",
            "product_form": "手串",
            "visual_observations": ["绿色调明显", "珠体排列成环"],
            "selling_points": ["近景实拍展示珠体轮廓"],
            "specification_note": "珠径 5 mm；尺寸由训练员测量",
            "quality_note": "图片可见颜色分布较均匀，仅为观察记录",
            "defect_note": "图片可见轻微表面纹理",
            "price_range": PriceRangeInput(
                minimum="1200.00",
                maximum="1500.00",
                basis="训练员录入的同品类价格标准",
            ),
            "price_confirmation": PriceConfirmationAudit(
                standard_code="PRICE-TURQUOISE-BRACELET-001",
                anchor_version=1,
                anchor_confirmed_by="synthetic-trainer-001",
                anchor_confirmed_at=datetime(2026, 8, 30, 7, 30, tzinfo=UTC),
                confirmed_by="synthetic-trainer-002",
                confirmed_at=datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
            ),
            "service_rules": [
                ServiceRuleInput(
                    service_code="scan_identity",
                    eligibility="购买后可用",
                    customer_summary="购买后可扫码进入身份与服务页面",
                )
            ],
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
            "created_at": datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
            "can_rollback": True,
        }
    )
    return base.model_copy(update=updates)


def test_complete_human_confirmed_record_is_allowed_for_local_pilot_only() -> None:
    decision = evaluate_pilot_release(training_version())

    assert decision.decision == "allowed_for_local_pilot"
    assert decision.reason_codes == []
    assert decision.local_synthetic_only is True
    assert decision.real_customer_data_allowed is False
    assert decision.external_actions_allowed is False


def test_missing_business_fields_require_human_input() -> None:
    decision = evaluate_pilot_release(
        training_version(
            specification_note="",
            price_range=None,
            inventory_status=None,
        )
    )

    assert decision.decision == "needs_human_input"
    assert decision.reason_codes == [
        "specification_missing",
        "price_range_missing",
        "inventory_missing",
    ]


def test_price_range_without_confirmation_audit_requires_human_input() -> None:
    decision = evaluate_pilot_release(training_version(price_confirmation=None))

    assert decision.decision == "needs_human_input"
    assert decision.reason_codes == ["price_confirmation_required"]


def test_legacy_price_audit_without_anchor_confirmation_requires_human_input() -> None:
    legacy_audit = PriceConfirmationAudit(
        standard_code="PRICE-TURQUOISE-BRACELET-001",
        anchor_version=1,
        confirmed_by="synthetic-trainer-002",
        confirmed_at=datetime(2026, 8, 30, 8, 0, tzinfo=UTC),
    )

    decision = evaluate_pilot_release(training_version(price_confirmation=legacy_audit))

    assert decision.decision == "needs_human_input"
    assert decision.reason_codes == ["price_confirmation_required"]


def test_rollback_price_audit_requires_fresh_human_confirmation() -> None:
    decision = evaluate_pilot_release(training_version(change_source="trainer_rollback"))

    assert decision.decision == "needs_human_input"
    assert decision.reason_codes == ["price_confirmation_required"]


def test_unconfirmed_classification_requires_human_input() -> None:
    decision = evaluate_pilot_release(
        training_version(
            change_source="system_initial",
            material_evidence="system_initial",
        )
    )

    assert decision.decision == "needs_human_input"
    assert decision.reason_codes == ["human_confirmation_required"]


def test_unsupported_customer_claim_is_blocked() -> None:
    decision = evaluate_pilot_release(training_version(selling_points=["官方认证正品并保证升值"]))

    assert decision.decision == "blocked"
    assert decision.reason_codes == ["forbidden_unsupported_claim"]
    assert decision.external_actions_allowed is False
