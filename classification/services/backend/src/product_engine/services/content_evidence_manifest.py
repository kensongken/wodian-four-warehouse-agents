from typing import Literal

from pydantic import BaseModel, ConfigDict

from product_engine.services.customer_safe_content import (
    UnsafeCustomerContentError,
    assert_customer_safe_content,
)
from product_engine.services.product_passport_drafts import ProductPassportDraft


class ContentEvidenceManifestFailure(ValueError):
    pass


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field_path: str
    evidence_level: Literal["image_observed", "merchant_declared"]
    public_summary: str
    evidence_refs: list[str]


class ContentEvidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["0.1-candidate"] = "0.1-candidate"
    template_code: str
    source_version: int
    claims: list[EvidenceClaim]
    withheld_fields: list[
        Literal[
            "material",
            "authenticity",
            "origin",
            "professional_quality_grade",
            "certificate_conclusion",
        ]
    ]
    candidate_only: Literal[True] = True
    automatic_publication_allowed: Literal[False] = False


def build_content_evidence_manifest(
    draft: ProductPassportDraft,
) -> ContentEvidenceManifest:
    customer_fields = [
        draft.approved_attributes.form,
        *draft.approved_attributes.visual_observations,
        *draft.approved_attributes.selling_points,
        draft.specification_note or "",
        draft.quality_note or "",
        draft.defect_note or "",
    ]
    if draft.price_advisory is not None:
        customer_fields.append(draft.price_advisory.basis)
    customer_fields.extend(rule.customer_summary for rule in draft.service_rules)
    try:
        assert_customer_safe_content(customer_fields)
    except UnsafeCustomerContentError as error:
        raise ContentEvidenceManifestFailure("forbidden_unsupported_claim") from error

    evidence_ref = f"training-label:{draft.template_code}:v{draft.source_version}"
    claims: list[EvidenceClaim] = [
        EvidenceClaim(
            field_path="form",
            evidence_level="image_observed",
            public_summary=draft.approved_attributes.form,
            evidence_refs=[evidence_ref],
        )
    ]
    claims.extend(
        EvidenceClaim(
            field_path=f"visual_observations.{index}",
            evidence_level="image_observed",
            public_summary=observation,
            evidence_refs=[evidence_ref],
        )
        for index, observation in enumerate(draft.approved_attributes.visual_observations)
    )
    for field_path, value in (
        ("quality_note", draft.quality_note),
        ("defect_note", draft.defect_note),
    ):
        if value:
            claims.append(
                EvidenceClaim(
                    field_path=field_path,
                    evidence_level="image_observed",
                    public_summary=value,
                    evidence_refs=[evidence_ref],
                )
            )
    claims.extend(
        EvidenceClaim(
            field_path=f"selling_points.{index}",
            evidence_level="merchant_declared",
            public_summary=selling_point,
            evidence_refs=[evidence_ref],
        )
        for index, selling_point in enumerate(draft.approved_attributes.selling_points)
    )
    if draft.specification_note:
        claims.append(
            EvidenceClaim(
                field_path="specification_note",
                evidence_level="merchant_declared",
                public_summary=draft.specification_note,
                evidence_refs=[evidence_ref],
            )
        )
    if draft.price_advisory is not None:
        claims.append(
            EvidenceClaim(
                field_path="price_advisory",
                evidence_level="merchant_declared",
                public_summary=(
                    f"建议区间 {draft.price_advisory.currency} "
                    f"{draft.price_advisory.minimum}–{draft.price_advisory.maximum}；"
                    f"依据：{draft.price_advisory.basis}"
                ),
                evidence_refs=[evidence_ref],
            )
        )
    claims.extend(
        EvidenceClaim(
            field_path=f"service_rules.{index}",
            evidence_level="merchant_declared",
            public_summary=rule.customer_summary,
            evidence_refs=[evidence_ref],
        )
        for index, rule in enumerate(draft.service_rules)
    )
    return ContentEvidenceManifest(
        template_code=draft.template_code,
        source_version=draft.source_version,
        claims=claims,
        withheld_fields=[
            "material",
            "authenticity",
            "origin",
            "professional_quality_grade",
            "certificate_conclusion",
        ],
    )
