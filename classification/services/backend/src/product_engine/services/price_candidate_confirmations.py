from collections.abc import Callable
from datetime import UTC, datetime

from product_engine.services.conservative_pricing import ConservativePriceCandidate, PriceAnchor
from product_engine.services.internal_product_content_package import (
    InternalProductContentPackageFailure,
    build_internal_product_content_package,
)
from product_engine.services.product_passport_drafts import (
    ProductPassportDraft,
    build_product_passport_draft,
)
from product_engine.services.training_label_versions import (
    PriceConfirmationAudit,
    PriceRangeInput,
    TrainingLabelFailure,
    TrainingLabelInput,
    TrainingLabelVersion,
    TrainingLabelVersionStore,
)


class PriceCandidateConfirmationFailure(ValueError):
    pass


def confirm_conservative_price_candidate(
    *,
    store: TrainingLabelVersionStore,
    draft: ProductPassportDraft,
    price_candidate: ConservativePriceCandidate,
    price_anchor: PriceAnchor,
    base_version: int,
    confirmed_by: str,
    confirmed_at: datetime | None = None,
    before_append: Callable[[TrainingLabelVersion], None] | None = None,
) -> TrainingLabelVersion:
    confirmer = confirmed_by.strip()
    if not confirmer:
        raise PriceCandidateConfirmationFailure("confirmed_by_required")
    confirmation_time = confirmed_at or datetime.now(UTC)
    if confirmation_time.tzinfo is None or confirmation_time.utcoffset() is None:
        raise PriceCandidateConfirmationFailure("confirmed_at_timezone_required")

    current = store.current(draft.template_code)
    if (
        current is None
        or current.version != base_version
        or draft.source_version != base_version
        or draft.source_review.version_id != current.version_id
    ):
        raise PriceCandidateConfirmationFailure("version_conflict")
    current_draft = build_product_passport_draft(current)
    if draft.model_dump(mode="json") != current_draft.model_dump(mode="json"):
        raise PriceCandidateConfirmationFailure("current_draft_mismatch")

    try:
        build_internal_product_content_package(draft, price_candidate, price_anchor)
    except InternalProductContentPackageFailure as error:
        raise PriceCandidateConfirmationFailure(str(error)) from error

    values = TrainingLabelInput(**current.model_dump()).model_copy(
        update={
            "price_range": PriceRangeInput(
                minimum=str(price_candidate.minimum),
                maximum=str(price_candidate.maximum),
                basis=price_candidate.basis,
            )
        }
    )
    audit = PriceConfirmationAudit(
        standard_code=price_candidate.standard_code,
        anchor_version=price_candidate.anchor_version,
        anchor_confirmed_by=price_candidate.confirmed_by,
        anchor_confirmed_at=price_candidate.confirmed_at,
        confirmed_by=confirmer,
        confirmed_at=confirmation_time,
    )
    try:
        return store.save_confirmed_price(
            draft.template_code,
            base_version,
            values,
            audit,
            before_append=before_append,
        )
    except TrainingLabelFailure as error:
        raise PriceCandidateConfirmationFailure(str(error)) from error
