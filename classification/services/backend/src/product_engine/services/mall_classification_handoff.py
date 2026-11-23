from typing import Literal

from pydantic import BaseModel, ConfigDict

from product_engine.services.customer_safe_content import (
    UnsafeCustomerContentError,
    assert_customer_safe_content,
)
from product_engine.services.material_classification import ClassificationDecision
from product_engine.services.product_passport_drafts import (
    BlockerCode,
    DraftMainImage,
    DraftVerificationBoundaries,
    ProductPassportDraft,
)


class MallClassificationHandoffFailure(ValueError):
    pass


class MallClassificationHandoff(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1"] = "0.1"
    template_code: str
    source_version: int
    material_classification: ClassificationDecision
    main_image: DraftMainImage | None
    verification_boundaries: DraftVerificationBoundaries
    remaining_blockers: list[BlockerCode]
    classification_ready_for_mall: Literal[True] = True
    sales_readiness_review: Literal["rejected", "passed"]
    review_scope: Literal["product_data_completeness_only"] = "product_data_completeness_only"
    product_publication_allowed: Literal[False] = False


def build_mall_classification_handoff(
    draft: ProductPassportDraft,
) -> MallClassificationHandoff:
    customer_fields = [
        draft.approved_attributes.material,
        draft.approved_attributes.form,
        *draft.approved_attributes.visual_observations,
        *draft.approved_attributes.selling_points,
        draft.specification_note or "",
        draft.quality_note or "",
        draft.defect_note or "",
    ]
    if draft.price_advisory is not None:
        customer_fields.append(draft.price_advisory.basis)
    for rule in draft.service_rules:
        customer_fields.extend([rule.service_code, rule.eligibility, rule.customer_summary])
    try:
        assert_customer_safe_content(customer_fields)
    except UnsafeCustomerContentError as error:
        raise MallClassificationHandoffFailure("forbidden_unsupported_claim") from error

    classification = draft.material_classification
    if (
        classification.decision_status != "human_confirmed"
        or classification.review_required
        or not classification.mall_import_allowed
    ):
        raise MallClassificationHandoffFailure("classification_not_human_confirmed")
    remaining_blockers = [blocker.code for blocker in draft.blockers]
    sales_readiness_review: Literal["rejected", "passed"] = (
        "rejected" if remaining_blockers else "passed"
    )
    return MallClassificationHandoff(
        template_code=draft.template_code,
        source_version=draft.source_version,
        material_classification=classification,
        main_image=draft.main_image,
        verification_boundaries=draft.verification_boundaries,
        remaining_blockers=remaining_blockers,
        sales_readiness_review=sales_readiness_review,
    )
