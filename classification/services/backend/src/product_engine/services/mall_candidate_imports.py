from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from product_engine.services.mall_classification_handoff import (
    MallClassificationHandoff,
    build_mall_classification_handoff,
)
from product_engine.services.material_classification import ClassificationDecision
from product_engine.services.product_passport_drafts import (
    ApprovedProductAttributes,
    BlockerCode,
    DraftMainImage,
    DraftPriceAdvisory,
    DraftSourceReview,
    DraftVerificationBoundaries,
    ProductPassportDraft,
)
from product_engine.services.training_label_versions import (
    InventoryStatus,
    ProductType,
    ServiceRuleInput,
)


class MallCandidateImportFailure(ValueError):
    def __init__(self, remaining_blockers: list[BlockerCode]) -> None:
        super().__init__("sales_readiness_not_passed")
        self.remaining_blockers = remaining_blockers


class MallCandidateImportPackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1-candidate"] = "0.1-candidate"
    template_code: str
    source_version: int
    source_review: DraftSourceReview
    product_type: ProductType
    inventory_status: InventoryStatus
    approved_attributes: ApprovedProductAttributes
    material_classification: ClassificationDecision
    main_image: DraftMainImage
    specification_note: str
    quality_note: str | None
    defect_note: str | None
    price_advisory: DraftPriceAdvisory
    service_rules: list[ServiceRuleInput]
    verification_boundaries: DraftVerificationBoundaries
    sales_readiness_review: Literal["passed"] = "passed"
    review_scope: Literal["product_data_completeness_only"] = "product_data_completeness_only"
    candidate_only: Literal[True] = True
    automatic_publication_allowed: Literal[False] = False
    warehouse_verification_required: Literal[True] = True


def build_mall_candidate_import(
    draft: ProductPassportDraft,
) -> MallCandidateImportPackage:
    handoff: MallClassificationHandoff = build_mall_classification_handoff(draft)
    if handoff.sales_readiness_review != "passed":
        raise MallCandidateImportFailure(handoff.remaining_blockers)

    return MallCandidateImportPackage(
        template_code=draft.template_code,
        source_version=draft.source_version,
        source_review=draft.source_review,
        product_type=cast(ProductType, draft.product_type),
        inventory_status=cast(InventoryStatus, draft.inventory_status),
        approved_attributes=draft.approved_attributes,
        material_classification=draft.material_classification,
        main_image=cast(DraftMainImage, draft.main_image),
        specification_note=cast(str, draft.specification_note),
        quality_note=draft.quality_note,
        defect_note=draft.defect_note,
        price_advisory=cast(DraftPriceAdvisory, draft.price_advisory),
        service_rules=draft.service_rules,
        verification_boundaries=draft.verification_boundaries,
    )
