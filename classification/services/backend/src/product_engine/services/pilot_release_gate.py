from typing import Literal

from pydantic import BaseModel, ConfigDict

from product_engine.services.product_passport_drafts import (
    ProductPassportDraftFailure,
    build_product_passport_draft,
)
from product_engine.services.training_label_versions import TrainingLabelVersion


class PilotReleaseDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1"] = "0.1"
    template_code: str
    source_version: int | None
    decision: Literal[
        "allowed_for_local_pilot",
        "needs_human_input",
        "blocked",
    ]
    reason_codes: list[str]
    local_synthetic_only: Literal[True] = True
    real_customer_data_allowed: Literal[False] = False
    external_actions_allowed: Literal[False] = False


def evaluate_pilot_release(record: TrainingLabelVersion) -> PilotReleaseDecision:
    try:
        draft = build_product_passport_draft(record)
    except ProductPassportDraftFailure as error:
        reason = str(error)
        decision: Literal["needs_human_input", "blocked"] = (
            "blocked" if reason == "forbidden_unsupported_claim" else "needs_human_input"
        )
        return PilotReleaseDecision(
            template_code=record.template_code,
            source_version=record.version,
            decision=decision,
            reason_codes=[reason],
        )

    reason_codes: list[str] = [blocker.code for blocker in draft.blockers]
    price_confirmation = record.price_confirmation
    if draft.price_advisory is not None and (
        record.change_source != "trainer_confirmed"
        or price_confirmation is None
        or price_confirmation.anchor_confirmed_by is None
        or price_confirmation.anchor_confirmed_at is None
    ):
        reason_codes.append("price_confirmation_required")
    return PilotReleaseDecision(
        template_code=record.template_code,
        source_version=record.version,
        decision=("needs_human_input" if reason_codes else "allowed_for_local_pilot"),
        reason_codes=reason_codes,
    )
