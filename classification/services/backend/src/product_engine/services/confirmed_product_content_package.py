from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from product_engine.services.content_evidence_manifest import (
    ContentEvidenceManifest,
    build_content_evidence_manifest,
)
from product_engine.services.product_passport_drafts import (
    ProductPassportDraft,
    ProductPassportDraftFailure,
    build_product_passport_draft,
)
from product_engine.services.training_label_versions import (
    PriceConfirmationAudit,
    TrainingLabelVersion,
)


class ConfirmedProductContentPackageFailure(ValueError):
    pass


class ConfirmedProductContentPackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1-internal-confirmed"] = "0.1-internal-confirmed"
    template_code: str
    source_version: int
    draft: ProductPassportDraft
    content_evidence: ContentEvidenceManifest
    price_confirmation: PriceConfirmationAudit
    price_review_status: Literal["human_confirmed"] = "human_confirmed"
    publication_status: Literal["blocked"] = "blocked"
    sales_journey_usage: Literal["preview_only"] = "preview_only"
    automatic_publication_allowed: Literal[False] = False


def build_confirmed_product_content_package(
    record: TrainingLabelVersion,
) -> ConfirmedProductContentPackage:
    try:
        record = TrainingLabelVersion.model_validate(record.model_dump())
    except ValidationError as error:
        raise ConfirmedProductContentPackageFailure("invalid_training_version") from error
    if record.change_source != "trainer_confirmed":
        raise ConfirmedProductContentPackageFailure("price_confirmation_version_required")
    if record.price_confirmation is None:
        raise ConfirmedProductContentPackageFailure("price_confirmation_required")
    try:
        draft = build_product_passport_draft(record)
    except ProductPassportDraftFailure as error:
        raise ConfirmedProductContentPackageFailure(str(error)) from error
    if draft.price_advisory is None:
        raise ConfirmedProductContentPackageFailure("confirmed_price_required")
    if "price_range_missing" in [blocker.code for blocker in draft.blockers]:
        raise ConfirmedProductContentPackageFailure("confirmed_price_blocker_present")

    evidence = build_content_evidence_manifest(draft)
    claims = []
    for claim in evidence.claims:
        if claim.field_path == "price_advisory":
            claim = claim.model_copy(
                update={
                    "evidence_refs": [
                        *claim.evidence_refs,
                        (
                            "price-anchor:"
                            f"{record.price_confirmation.standard_code}:"
                            f"v{record.price_confirmation.anchor_version}"
                        ),
                        f"price-confirmation:{record.template_code}:v{record.version}",
                    ]
                }
            )
        claims.append(claim)
    evidence = evidence.model_copy(update={"claims": claims})

    return ConfirmedProductContentPackage(
        template_code=record.template_code,
        source_version=record.version,
        draft=draft,
        content_evidence=evidence,
        price_confirmation=record.price_confirmation,
    )
