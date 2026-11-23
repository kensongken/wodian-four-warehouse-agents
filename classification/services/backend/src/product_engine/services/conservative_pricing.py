from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from product_engine.services.customer_safe_content import (
    UnsafeCustomerContentError,
    assert_customer_safe_content,
)
from product_engine.services.material_classification import CategoryCode
from product_engine.services.product_passport_drafts import ProductPassportDraft


class ConservativePricingFailure(ValueError):
    pass


class PriceAnchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1"] = "0.1"
    standard_code: str = Field(min_length=1)
    category_code: CategoryCode
    form: str = Field(min_length=1)
    specification_summary: str = Field(min_length=1)
    quality_summary: str = ""
    defect_summary: str = ""
    currency: Literal["CNY"] = "CNY"
    minimum: Decimal
    maximum: Decimal
    basis: str = Field(min_length=1)
    evidence_level: Literal["merchant_declared"] = "merchant_declared"
    sample_size: Literal[1] = 1
    anchor_version: int = Field(ge=1)
    confirmation_source: Literal["trainer_confirmed"]
    confirmed_by: str = Field(min_length=1)
    confirmed_at: datetime

    @field_validator(
        "standard_code",
        "form",
        "specification_summary",
        "quality_summary",
        "defect_summary",
        "basis",
        "confirmed_by",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if not (self.quality_summary or self.defect_summary):
            raise ValueError("price_anchor_quality_or_defect_required")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise ValueError("price_anchor_confirmation_timezone_required")
        if (
            not self.minimum.is_finite()
            or not self.maximum.is_finite()
            or self.minimum < 0
            or self.maximum < 0
            or self.minimum >= self.maximum
        ):
            raise ValueError("invalid_price_anchor_range")
        return self


class ConservativePriceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1-candidate"] = "0.1-candidate"
    template_code: str
    source_version: int
    standard_code: str
    category_code: CategoryCode
    form: str
    currency: Literal["CNY"] = "CNY"
    minimum: Decimal
    maximum: Decimal
    basis: str
    evidence_level: Literal["merchant_declared"] = "merchant_declared"
    confidence: Literal["low_single_anchor"] = "low_single_anchor"
    sample_size: Literal[1] = 1
    anchor_version: int
    confirmation_source: Literal["trainer_confirmed"]
    confirmed_by: str
    confirmed_at: datetime
    recommended_exact_price: Literal[None] = None
    candidate_only: Literal[True] = True
    automatic_publication_allowed: Literal[False] = False

    @field_validator(
        "template_code", "standard_code", "form", "basis", "confirmed_by", mode="before"
    )
    @classmethod
    def strip_candidate_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_candidate_boundary(self) -> Self:
        if not all(
            (self.template_code, self.standard_code, self.form, self.basis, self.confirmed_by)
        ):
            raise ValueError("invalid_price_candidate_text")
        if (
            not self.minimum.is_finite()
            or not self.maximum.is_finite()
            or self.minimum < 0
            or self.maximum < 0
            or self.minimum >= self.maximum
        ):
            raise ValueError("invalid_price_candidate_range")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise ValueError("price_candidate_confirmation_timezone_required")
        try:
            assert_customer_safe_content([self.form, self.basis])
        except UnsafeCustomerContentError as error:
            raise ValueError("forbidden_unsupported_claim") from error
        return self


def build_conservative_price_candidate(
    draft: ProductPassportDraft,
    anchor: PriceAnchor,
) -> ConservativePriceCandidate:
    try:
        anchor = PriceAnchor.model_validate(anchor.model_dump())
    except ValidationError as error:
        raise ConservativePricingFailure("invalid_price_anchor") from error
    classification = draft.material_classification
    if (
        classification.decision_status != "human_confirmed"
        or classification.review_required
        or classification.category_code == "information_insufficient"
    ):
        raise ConservativePricingFailure("product_category_not_human_confirmed")
    if classification.category_code != anchor.category_code:
        raise ConservativePricingFailure("price_anchor_category_mismatch")
    if draft.approved_attributes.form.strip() != anchor.form.strip():
        raise ConservativePricingFailure("price_anchor_form_mismatch")
    if not (draft.specification_note or "").strip() or not (
        (draft.quality_note or "").strip() or (draft.defect_note or "").strip()
    ):
        raise ConservativePricingFailure("product_price_factors_incomplete")
    try:
        assert_customer_safe_content(
            [
                anchor.form,
                anchor.specification_summary,
                anchor.quality_summary,
                anchor.defect_summary,
                anchor.basis,
            ]
        )
    except UnsafeCustomerContentError as error:
        raise ConservativePricingFailure("forbidden_unsupported_claim") from error
    return ConservativePriceCandidate(
        template_code=draft.template_code,
        source_version=draft.source_version,
        standard_code=anchor.standard_code,
        category_code=anchor.category_code,
        form=anchor.form,
        minimum=anchor.minimum,
        maximum=anchor.maximum,
        basis=anchor.basis,
        anchor_version=anchor.anchor_version,
        confirmation_source=anchor.confirmation_source,
        confirmed_by=anchor.confirmed_by,
        confirmed_at=anchor.confirmed_at,
    )
