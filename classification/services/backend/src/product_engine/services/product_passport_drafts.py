from datetime import datetime
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict

from product_engine.services.customer_safe_content import (
    UnsafeCustomerContentError,
    assert_customer_safe_content,
)
from product_engine.services.material_classification import (
    ClassificationDecision,
    classify_trainer_label,
)
from product_engine.services.training_label_versions import (
    InventoryStatus,
    ProductType,
    ServiceRuleInput,
    TrainingLabelVersion,
)


class ProductPassportDraftFailure(ValueError):
    pass


class ApprovedProductAttributes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    material: str
    form: str
    visual_observations: list[str]
    selling_points: list[str]


class DraftSourceReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_type: Literal["human"] = "human"
    version_id: UUID
    change_source: Literal["trainer_confirmed", "trainer_rollback"]
    reviewed_at: datetime
    training_state: Literal["label_confirmed_model_not_trained"]


class DraftVerificationBoundaries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    authenticity: Literal["not_verified"]
    origin: Literal["not_verified"]
    quality: Literal["not_verified"]
    warehouse: Literal["pending_warehouse_verification"]


class DraftPriceAdvisory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: Literal["CNY"] = "CNY"
    minimum: str
    maximum: str
    basis: str
    semantics: Literal["advisory_range_not_fixed_price"] = "advisory_range_not_fixed_price"


class DraftMainImage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template_code: str
    role: Literal["main"] = "main"
    source: Literal["album_classified_image"] = "album_classified_image"
    review_state: Literal["human_confirmed"] = "human_confirmed"


BlockerCode = Literal[
    "material_classification_missing",
    "main_image_missing",
    "main_image_mismatch",
    "product_type_missing",
    "specification_missing",
    "quality_defect_review_missing",
    "price_range_missing",
    "inventory_missing",
    "service_rules_missing",
]


class DraftBlocker(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: BlockerCode
    message_zh: str
    required_action_zh: str


class ProductPassportDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    draft_id: UUID
    contract_version: Literal["0.1-draft"] = "0.1-draft"
    template_code: str
    source_version: int
    product_type: ProductType | None
    inventory_status: InventoryStatus | None
    source_review: DraftSourceReview
    approved_attributes: ApprovedProductAttributes
    material_classification: ClassificationDecision
    main_image: DraftMainImage | None
    specification_note: str | None
    quality_note: str | None
    defect_note: str | None
    service_rules: list[ServiceRuleInput]
    verification_boundaries: DraftVerificationBoundaries
    price_advisory: DraftPriceAdvisory | None
    blockers: list[DraftBlocker]
    publication_status: Literal["blocked"] = "blocked"
    mall_import_allowed: Literal[False] = False
    sales_journey_usage: Literal["preview_only"] = "preview_only"


def _blocker(
    code: BlockerCode,
    message_zh: str,
    required_action_zh: str,
) -> DraftBlocker:
    return DraftBlocker(
        code=code,
        message_zh=message_zh,
        required_action_zh=required_action_zh,
    )


def build_product_passport_draft(
    record: TrainingLabelVersion,
) -> ProductPassportDraft:
    customer_fields = [
        record.material_label,
        record.product_form,
        *record.visual_observations,
        *record.selling_points,
        record.specification_note,
        record.quality_note,
        record.defect_note,
    ]
    if record.price_range is not None:
        customer_fields.append(record.price_range.basis)
    for rule in record.service_rules:
        customer_fields.extend([rule.service_code, rule.eligibility, rule.customer_summary])
    try:
        assert_customer_safe_content(customer_fields)
    except UnsafeCustomerContentError as error:
        raise ProductPassportDraftFailure("forbidden_unsupported_claim") from error

    if record.change_source not in {"trainer_confirmed", "trainer_rollback"}:
        raise ProductPassportDraftFailure("human_confirmation_required")
    human_change_source = cast(
        Literal["trainer_confirmed", "trainer_rollback"],
        record.change_source,
    )

    material_classification = classify_trainer_label(
        record.material_label,
        version_id=record.version_id,
        template_code=record.template_code,
    )
    blockers: list[DraftBlocker] = []
    if material_classification.decision_status != "human_confirmed":
        blockers.append(
            _blocker(
                "material_classification_missing",
                "当前材质标签不在首批六品类内，商品资料保持信息不足。",
                "由训练员在沉香、绿松石、南红、和田玉、彩宝、翡翠中确认，或保留信息不足。",
            )
        )
    main_image: DraftMainImage | None = None
    if record.main_image_template_code is None:
        blockers.append(
            _blocker(
                "main_image_missing",
                "尚未确认用于商品资料的主图。",
                "由训练员确认当前相册商品图是否作为主图。",
            )
        )
    elif record.main_image_template_code != record.template_code:
        blockers.append(
            _blocker(
                "main_image_mismatch",
                "主图引用不属于当前相册商品。",
                "重新选择当前商品编码下的图片作为主图。",
            )
        )
    else:
        main_image = DraftMainImage(template_code=record.main_image_template_code)
    if record.product_type is None:
        blockers.append(
            _blocker(
                "product_type_missing",
                "商品类型尚未由人工选择。",
                "在首售身份珠、旧料或改款成品中选择一种商品类型。",
            )
        )
    if not record.specification_note.strip():
        blockers.append(
            _blocker(
                "specification_missing",
                "商品规格尚未录入。",
                "由训练员测量并录入尺寸、珠径或其他适用品类规格。",
            )
        )
    if not record.quality_note.strip() and not record.defect_note.strip():
        blockers.append(
            _blocker(
                "quality_defect_review_missing",
                "品质与瑕疵观察尚未完成。",
                "由训练员分别记录可见品质特征和可见瑕疵，不作鉴定结论。",
            )
        )
    if record.price_range is None:
        blockers.append(
            _blocker(
                "price_range_missing",
                "人工建议价格区间尚未录入。",
                "按品类价格标准录入建议区间及依据，不得生成固定价格承诺。",
            )
        )
    if record.inventory_status is None:
        blockers.append(
            _blocker(
                "inventory_missing",
                "库存状态尚未接入。",
                "接入商家确认的可售库存状态。",
            )
        )
    if not record.service_rules:
        blockers.append(
            _blocker(
                "service_rules_missing",
                "客户可见服务规则尚未接入。",
                "补充退换、扫码服务或改款服务等客户可见规则。",
            )
        )

    price_advisory = None
    if record.price_range is not None:
        price_advisory = DraftPriceAdvisory(
            minimum=record.price_range.minimum,
            maximum=record.price_range.maximum,
            basis=record.price_range.basis,
        )

    return ProductPassportDraft(
        draft_id=uuid5(
            NAMESPACE_URL,
            f"product-passport-draft:{record.template_code}:v{record.version}",
        ),
        template_code=record.template_code,
        source_version=record.version,
        product_type=record.product_type,
        inventory_status=record.inventory_status,
        source_review=DraftSourceReview(
            version_id=record.version_id,
            change_source=human_change_source,
            reviewed_at=record.created_at,
            training_state=record.training_state,
        ),
        approved_attributes=ApprovedProductAttributes(
            material=record.material_label,
            form=record.product_form,
            visual_observations=record.visual_observations,
            selling_points=record.selling_points,
        ),
        material_classification=material_classification,
        main_image=main_image,
        specification_note=record.specification_note or None,
        quality_note=record.quality_note or None,
        defect_note=record.defect_note or None,
        service_rules=record.service_rules,
        verification_boundaries=DraftVerificationBoundaries(
            authenticity=record.authenticity_verification,
            origin=record.origin_verification,
            quality=record.quality_verification,
            warehouse=record.warehouse_verification,
        ),
        price_advisory=price_advisory,
        blockers=blockers,
    )
