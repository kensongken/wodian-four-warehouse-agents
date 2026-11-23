from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from product_engine.services.conservative_pricing import (
    ConservativePriceCandidate,
    ConservativePricingFailure,
    PriceAnchor,
    build_conservative_price_candidate,
)
from product_engine.services.content_evidence_manifest import (
    ContentEvidenceManifest,
    EvidenceClaim,
    build_content_evidence_manifest,
)
from product_engine.services.product_passport_drafts import ProductPassportDraft


class InternalProductContentPackageFailure(ValueError):
    pass


class InternalProductContentPackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1-internal-candidate"] = "0.1-internal-candidate"
    template_code: str
    source_version: int
    draft: ProductPassportDraft
    content_evidence: ContentEvidenceManifest
    conservative_price_candidate: ConservativePriceCandidate
    price_review_status: Literal["pending_human_confirmation"] = "pending_human_confirmation"
    publication_status: Literal["blocked"] = "blocked"
    sales_journey_usage: Literal["preview_only"] = "preview_only"
    automatic_publication_allowed: Literal[False] = False


def build_internal_product_content_package(
    draft: ProductPassportDraft,
    price_candidate: ConservativePriceCandidate,
    price_anchor: PriceAnchor,
) -> InternalProductContentPackage:
    try:
        draft = ProductPassportDraft.model_validate(draft.model_dump())
    except ValidationError as error:
        raise InternalProductContentPackageFailure("invalid_product_passport_draft") from error
    if draft.price_advisory is not None:
        raise InternalProductContentPackageFailure("formal_price_already_present")
    if "price_range_missing" not in [blocker.code for blocker in draft.blockers]:
        raise InternalProductContentPackageFailure("formal_price_review_blocker_missing")

    if draft.template_code != price_candidate.template_code:
        raise InternalProductContentPackageFailure("price_candidate_template_mismatch")
    if draft.source_version != price_candidate.source_version:
        raise InternalProductContentPackageFailure("price_candidate_source_version_mismatch")
    if draft.material_classification.category_code != price_candidate.category_code:
        raise InternalProductContentPackageFailure("price_candidate_category_mismatch")
    if draft.approved_attributes.form != price_candidate.form:
        raise InternalProductContentPackageFailure("price_candidate_form_mismatch")

    try:
        price_candidate = ConservativePriceCandidate.model_validate(price_candidate.model_dump())
    except ValidationError as error:
        raise InternalProductContentPackageFailure(
            "invalid_conservative_price_candidate"
        ) from error

    try:
        expected_candidate = build_conservative_price_candidate(draft, price_anchor)
    except ConservativePricingFailure as error:
        raise InternalProductContentPackageFailure("invalid_price_anchor") from error
    if price_candidate.model_dump(mode="json") != expected_candidate.model_dump(mode="json"):
        raise InternalProductContentPackageFailure("invalid_conservative_price_candidate")

    evidence = build_content_evidence_manifest(draft)
    price_claim = EvidenceClaim(
        field_path="conservative_price_candidate",
        evidence_level="merchant_declared",
        public_summary=(
            f"待人工确认区间 {price_candidate.currency} "
            f"{price_candidate.minimum}–{price_candidate.maximum}；"
            f"依据：{price_candidate.basis}"
        ),
        evidence_refs=[
            f"price-anchor:{price_candidate.standard_code}:v{price_candidate.anchor_version}"
        ],
    )
    evidence = evidence.model_copy(update={"claims": [*evidence.claims, price_claim]})

    return InternalProductContentPackage(
        template_code=draft.template_code,
        source_version=draft.source_version,
        draft=draft,
        content_evidence=evidence,
        conservative_price_candidate=price_candidate,
    )
