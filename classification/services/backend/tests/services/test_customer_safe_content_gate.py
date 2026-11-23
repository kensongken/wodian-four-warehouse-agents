from datetime import UTC, datetime
from uuid import uuid4

import pytest

from product_engine.services.mall_classification_handoff import (
    MallClassificationHandoffFailure,
    build_mall_classification_handoff,
)
from product_engine.services.product_passport_drafts import (
    ProductPassportDraftFailure,
    build_product_passport_draft,
)
from product_engine.services.training_label_versions import (
    PriceRangeInput,
    ServiceRuleInput,
    TrainingLabelFailure,
    TrainingLabelInput,
    TrainingLabelVersion,
    TrainingLabelVersionStore,
)


def safe_input(**updates) -> TrainingLabelInput:
    values = {
        "sha256": "a" * 64,
        "product_type": "first_sale_bead",
        "inventory_status": "in_stock",
        "material_label": "绿松",
        "product_form": "手串",
        "visual_observations": ["绿色调明显"],
        "selling_points": ["近景实拍展示珠体轮廓"],
        "specification_note": "珠径 5 mm；由训练员测量",
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
    values.update(updates)
    return TrainingLabelInput.model_validate(values)


def safe_version(**updates) -> TrainingLabelVersion:
    values = {
        **safe_input().model_dump(),
        "version_id": uuid4(),
        "template_code": "LOCAL-GREEN-BRACELET",
        "version": 2,
        "previous_version_id": uuid4(),
        "rollback_target_version_id": None,
        "change_source": "trainer_confirmed",
        "material_evidence": "trainer_confirmed",
        "created_at": datetime(2026, 8, 18, 6, 30, tzinfo=UTC),
    }
    values.update(updates)
    return TrainingLabelVersion.model_validate(values)


def unsafe_update(field_name: str, text: str) -> dict:
    direct_text_fields = {
        "material_label",
        "product_form",
        "specification_note",
        "quality_note",
        "defect_note",
    }
    if field_name in direct_text_fields:
        return {field_name: text}
    if field_name in {"visual_observations", "selling_points"}:
        return {field_name: [text]}
    if field_name == "price_basis":
        return {
            "price_range": PriceRangeInput(
                minimum="1200.00",
                maximum="1500.00",
                basis=text,
            )
        }
    service_values = {
        "service_code": "scan_identity",
        "eligibility": "购买后可用",
        "customer_summary": "购买后可扫码进入服务页面",
    }
    service_values[field_name] = text
    return {"service_rules": [ServiceRuleInput.model_validate(service_values)]}


@pytest.mark.parametrize(
    ("field_name", "unsafe_text"),
    [
        ("selling_points", "经鉴定为天然A货"),
        ("visual_observations", "经 鉴 定 为 天 然 Ａ 货"),
        ("quality_note", "固定价\u200b格1200元"),
        ("defect_note", "支 持 回 购"),
        ("price_basis", "固　定　价　格 1200 元"),
        ("customer_summary", "系统自动微\u200b信发送服务通知"),
        ("eligibility", "支\u2060持回购"),
        ("service_code", "system auto wechat send"),
        ("material_label", "正 品 绿松"),
        ("product_form", "保\u200d值手串"),
        ("specification_note", "产 自 保山"),
    ],
)
def test_every_customer_field_rejects_unsafe_semantics_before_append(
    tmp_path,
    field_name,
    unsafe_text,
) -> None:
    version_path = tmp_path / "versions.jsonl"
    initial_path = tmp_path / "initial.jsonl"
    store = TrainingLabelVersionStore(version_path, initial_path)
    store.register_initial("LOCAL-GREEN-BRACELET", safe_input())
    before = initial_path.read_bytes()

    with pytest.raises(TrainingLabelFailure, match="forbidden_unsupported_claim"):
        store.save(
            "LOCAL-GREEN-BRACELET",
            1,
            safe_input(**unsafe_update(field_name, unsafe_text)),
        )

    assert not version_path.exists()
    assert initial_path.read_bytes() == before


def test_historical_unsafe_version_fails_closed_when_read(tmp_path) -> None:
    version_path = tmp_path / "versions.jsonl"
    unsafe = safe_version(selling_points=["支持回购"])
    version_path.write_text(unsafe.model_dump_json() + "\n", encoding="utf-8")
    store = TrainingLabelVersionStore(version_path, tmp_path / "initial.jsonl")

    with pytest.raises(TrainingLabelFailure, match="forbidden_unsupported_claim"):
        store.current("LOCAL-GREEN-BRACELET")


def test_draft_builder_revalidates_constructed_unsafe_version() -> None:
    unsafe = safe_version(
        service_rules=[
            ServiceRuleInput(
                service_code="scan_identity",
                eligibility="购买后可用",
                customer_summary="系统自动微信发送服务通知",
            )
        ]
    )

    with pytest.raises(
        ProductPassportDraftFailure,
        match="forbidden_unsupported_claim",
    ):
        build_product_passport_draft(unsafe)


def test_handoff_revalidates_unsafe_draft_instead_of_passing_readiness() -> None:
    safe_draft = build_product_passport_draft(safe_version())
    unsafe_attributes = safe_draft.approved_attributes.model_copy(
        update={"selling_points": ["固定价格1200元"]}
    )
    unsafe_draft = safe_draft.model_copy(update={"approved_attributes": unsafe_attributes})

    with pytest.raises(
        MallClassificationHandoffFailure,
        match="forbidden_unsupported_claim",
    ):
        build_mall_classification_handoff(unsafe_draft)
