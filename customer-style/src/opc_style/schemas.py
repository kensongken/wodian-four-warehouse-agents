"""HTTP contracts for customer-owned style samples."""

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

from opc_style.domain.analyzer import (
    STYLE_ANALYSIS_LIST_FIELDS,
    STYLE_FIELD_ENTRY_MAX_LENGTH,
    STYLE_FIELD_MAX_ITEMS,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_identity import (
    identity_card_payload,
    normalize_identity_draft,
)
from opc_style.domain.style_rules import SceneAspectRatio

type StyleFieldEntry = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=STYLE_FIELD_ENTRY_MAX_LENGTH,
    ),
]
type StyleFieldValues = Annotated[
    list[StyleFieldEntry], Field(max_length=STYLE_FIELD_MAX_ITEMS)
]

type ProductAssetOpaqueId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, strict=True, min_length=1, max_length=255),
]
type ProductAssetQualityFlag = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$",
    ),
]
type Sha256Hex = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
type PositiveImageDimension = Annotated[int, Field(strict=True, gt=0)]
type ImageOutputIdempotencyKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class ProductAssetIntakeRequest(BaseModel):
    """Strict metadata-only handoff for one organizer-confirmed product photo."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_id: ProductAssetOpaqueId
    photo_id: ProductAssetOpaqueId
    confirmed: StrictBool
    source_type: Literal[
        "photo_organizer", "synthetic_metadata", "mall_authorized_upload"
    ]
    quality_flags: list[ProductAssetQualityFlag] = Field(max_length=20)
    occurred_at: AwareDatetime
    transform_permission: StrictBool
    content_sha256: Sha256Hex
    mime_type: Literal["image/jpeg", "image/png"]
    width: PositiveImageDimension
    height: PositiveImageDimension

    @field_validator("quality_flags", mode="before")
    @classmethod
    def reject_quality_flag_whitespace(cls, value: object) -> object:
        """Reject raw whitespace before model-level string trimming can hide it."""
        if isinstance(value, list) and any(
            isinstance(flag, str) and any(character.isspace() for character in flag)
            for flag in value
        ):
            raise ValueError("quality flags must not contain whitespace")
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def require_rfc3339_json_string(cls, value: object) -> str:
        """Reject Pydantic's numeric timestamp coercion at the HTTP boundary."""
        if (
            not isinstance(value, str)
            or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
                r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                value,
            )
            is None
        ):
            raise ValueError("occurred_at must be an RFC3339 string with timezone")
        return value

    @field_validator("product_id", "photo_id")
    @classmethod
    def reject_paths_and_embedded_media(cls, value: str) -> str:
        """Keep opaque references from becoming a server-path or media channel."""
        lowered = value.lower()
        if (
            "/" in value
            or "\\" in value
            or "://" in lowered
            or lowered.startswith("data:")
        ):
            raise ValueError("opaque identifiers must not contain paths or media")
        return value

    @model_validator(mode="after")
    def require_confirmation(self) -> "ProductAssetIntakeRequest":
        """Fail closed before unconfirmed metadata can cause persistence effects."""
        if not self.confirmed:
            raise ValueError("confirmed must be true")
        return self


class ProductAssetResponse(BaseModel):
    """Customer-visible asset facts plus event-derived readiness."""

    id: UUID
    product_id: str
    photo_id: str
    identity: str
    confirmed: bool
    source_type: str
    quality_flags: list[str]
    occurred_at: datetime
    transform_permission: bool | None
    content_sha256: str
    mime_type: str
    width: int
    height: int
    generation_ready: bool
    readiness_reasons: list[str]
    created_at: datetime


class ProductAssetCollectionResponse(BaseModel):
    """All visible source photos associated with one customer-owned product."""

    product_id: str
    assets: list[ProductAssetResponse]


class RealPhotoOutputRequest(BaseModel):
    """Identity-only request; storage and filesystem paths are never accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_id: ProductAssetOpaqueId
    asset_id: UUID
    profile: Literal["catalog-v1"]
    idempotency_key: ImageOutputIdempotencyKey

    @field_validator("product_id")
    @classmethod
    def reject_product_paths(cls, value: str) -> str:
        lowered = value.lower()
        if (
            "/" in value
            or "\\" in value
            or "://" in lowered
            or lowered.startswith("data:")
        ):
            raise ValueError("product identity must not contain paths or media")
        return value


class RealPhotoCandidateResponse(BaseModel):
    """Public derivative facts with state derived outside the immutable row."""

    id: UUID
    sequence_number: int
    identity: str
    candidate_state: str
    content_sha256: str
    mime_type: str
    width: int
    height: int
    adapter_version: str
    metadata: dict[str, object]


class RealPhotoOutputResponse(BaseModel):
    """Customer-visible status for one real-photo normalization job."""

    id: UUID
    product_id: str
    asset_id: UUID
    output_type: Literal["real_photo"]
    profile: str
    adapter_version: str
    status: str
    failure_reason: str | None
    publishable: Literal[False] = False
    candidates: list[RealPhotoCandidateResponse]
    created_at: datetime
    updated_at: datetime


class ImageGenerationSourceRequest(BaseModel):
    """One customer-selected product and its existing reference asset identity."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_id: ProductAssetOpaqueId
    asset_id: UUID

    @field_validator("product_id")
    @classmethod
    def reject_product_paths(cls, value: str) -> str:
        lowered = value.lower()
        if (
            "/" in value
            or "\\" in value
            or "://" in lowered
            or lowered.startswith("data:")
        ):
            raise ValueError("product identity must not contain paths or media")
        return value


class WearPresentationProfile(BaseModel):
    """Explicit system-virtual-model choices; never a real-person identity."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    position: Literal["auto", "hand", "wrist", "neck", "ear", "garment"] = "auto"
    skin_tone: Literal["light", "medium", "deep"] = "medium"
    gender_presentation: Literal["feminine", "masculine", "neutral"] = "neutral"
    age_presentation: Literal["adult", "mature"] = "adult"
    styling: Literal["minimal", "commercial", "elegant", "casual"] = "commercial"


class ImageGenerationOutputRequest(BaseModel):
    """Identity-only scene request with no media, path, or provider controls."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sources: list[ImageGenerationSourceRequest]
    scene_style_id: UUID
    style_card_version_id: UUID
    output_ratio: Literal["1:1", "4:5", "3:4", "16:9"]
    idempotency_key: ImageOutputIdempotencyKey
    slot_key: str = Field(default="legacy", min_length=1, max_length=100)
    subject_seal_id: UUID | None = None
    product_identity_card_version_id: UUID | None = None
    product_identity_card_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    product_identity_card: dict[str, object] | None = None
    wear_presentation: WearPresentationProfile | None = None


class ImageGenerationCandidateResponse(BaseModel):
    """Public synthetic candidate facts without internal artifact paths."""

    id: UUID
    output_job_id: UUID
    sequence_number: int
    identity: str
    candidate_state: str
    content_sha256: str
    mime_type: str
    width: int
    height: int
    adapter_version: str
    generation_mode: str
    synthetic_test_only: bool
    publishable: bool
    source_product_ids: list[str]
    source_asset_ids: list[UUID]
    style_card_version_id: UUID
    main_style_card_version_id: UUID | None
    scene_style_id: UUID
    protected_pixels_match: bool | None = None
    content_url: str | None = None


class ImageGenerationOutputResponse(BaseModel):
    """Customer-visible state for scene and multi-product synthetic jobs."""

    id: UUID
    output_type: Literal["scene", "multi_product", "product_style", "wear"]
    output_ratio: str
    adapter_version: str
    generation_mode: str
    synthetic_test_only: bool
    publishable: bool
    status: str
    failure_reason: str | None
    source_product_ids: list[str]
    source_asset_ids: list[UUID]
    style_card_version_id: UUID
    main_style_card_version_id: UUID | None
    scene_style_id: UUID
    subject_lock_state: str | None = None
    subject_seal_id: UUID | None = None
    original_content_url: str | None = None
    wear_presentation: dict[str, object] | None = None
    presentation_origin: Literal["system_virtual_model"] | None = None
    real_wear_photo: Literal[False] | None = None
    candidates: list[ImageGenerationCandidateResponse]
    created_at: datetime
    updated_at: datetime


class AuthenticityCheckResponse(BaseModel):
    """One immutable synthetic/local rule result, never a visual-AI verdict."""

    check_type: str
    attempt: int
    passed: bool
    reason: str
    checker_version: str
    details: dict[str, object]
    created_at: datetime


class AuthenticityCandidateResponse(BaseModel):
    """One candidate's event-derived gate state and append-only checks."""

    id: UUID
    sequence_number: int
    candidate_state: str
    checks: list[AuthenticityCheckResponse]


class AuthenticityGateResponse(BaseModel):
    """Job-level machine gate; passing does not approve or publish anything."""

    id: UUID
    status: str
    failure_reason: str | None
    checker_version: str
    check_mode: Literal["synthetic_local_rule_based"]
    visual_ai: Literal[False]
    customer_approved: Literal[False]
    publishable: Literal[False]
    candidates: list[AuthenticityCandidateResponse]
    updated_at: datetime


class ImageReviewRequest(BaseModel):
    """One immutable customer decision; no publication control is accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal[
        "approved", "not_my_style", "product_detail_inaccurate", "rejected"
    ]
    reason: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True, strict=True, min_length=1, max_length=255
            ),
        ]
        | None
    ) = None
    idempotency_key: ImageOutputIdempotencyKey
    fidelity_state: Literal["green", "yellow", "red", "unknown"] | None = None
    fidelity_flags: tuple[
        Literal[
            "subject_incomplete",
            "component_count_changed",
            "primary_color_changed",
            "core_hardware_changed",
            "product_form_changed",
            "product_category_changed",
            "component_shape_changed",
            "component_arrangement_changed",
            "core_accessory_changed",
            "provider_check_unavailable",
            "minor_color_drift",
            "minor_detail_uncertain",
            "background_style_drift",
        ],
        ...,
    ] = ()
    fidelity_warning_acknowledged: StrictBool = False


class RealPhotoImageReferenceResponse(BaseModel):
    """Immutable original real-photo evidence retained for a product."""

    image_kind: Literal["real_photo"]
    asset_id: UUID
    identity: str
    content_sha256: str
    mime_type: str
    width: int
    height: int


class ApprovedImageReferenceResponse(BaseModel):
    """An approved derivative reference that remains explicitly unpublished."""

    image_kind: Literal["real_photo", "style_image"]
    candidate_id: UUID
    identity: str
    content_sha256: str
    mime_type: str
    width: int
    height: int
    customer_approved: Literal[True]
    publishable: Literal[False]
    source_product_ids: list[str]
    source_asset_ids: list[UUID]


class ProductDualImageAssociationResponse(BaseModel):
    """One product's approved derivative plus its still-separate real source."""

    product_id: str
    approved_image: ApprovedImageReferenceResponse
    real_photo: RealPhotoImageReferenceResponse


class ImageReviewResponse(BaseModel):
    """Durable review outcome with source trace and no external publish action."""

    review_id: UUID
    job_id: UUID
    candidate_id: UUID
    decision: Literal[
        "approved", "not_my_style", "product_detail_inaccurate", "rejected"
    ]
    reason: str | None
    job_status: Literal[
        "awaiting_customer_review", "customer_approved", "customer_rejected"
    ]
    candidate_state: Literal["customer_approved", "customer_rejected"]
    image_kind: Literal["real_photo", "style_image"]
    customer_approved: bool
    publishable: Literal[False]
    source_product_ids: list[str]
    source_asset_ids: list[UUID]
    product_associations: list[ProductDualImageAssociationResponse]
    created_at: datetime
    suite_state: str | None = None


class LocalReviewCandidateResponse(BaseModel):
    """Browser-safe candidate data with nonce-protected content references."""

    candidate_id: UUID
    sequence_number: int
    content_sha256: str
    mime_type: str
    width: int
    height: int
    identity: str
    content_url: str
    publishable: Literal[False] = False


class LocalApprovedAssociationResponse(BaseModel):
    product_id: str
    candidate_id: UUID
    asset_id: UUID


class LocalReviewJobResponse(BaseModel):
    job_id: UUID
    handoff_id: UUID
    output_type: str
    job_status: str
    status: str
    session_id: UUID | None
    current_job_id: UUID
    regeneration_count: int
    candidates: list[LocalReviewCandidateResponse]
    source_product_ids: list[str]
    source_asset_ids: list[UUID]
    approved_candidate_id: UUID | None = None
    approved_associations: list[LocalApprovedAssociationResponse] = Field(
        default_factory=list
    )
    publishable: Literal[False] = False


class LocalReviewDecisionResponse(ImageReviewResponse):
    session_id: UUID
    next_job_id: UUID | None
    regeneration_count: int
    status: str
    outcome: Literal[
        "approved", "regenerated", "manual_cap_reached", "manual_generation_unavailable"
    ]
    current_session_status: str
    current_job_id: UUID
    current_regeneration_count: int


class StyleImageProductViewResponse(BaseModel):
    """Customer-approved style image chosen as a product's default view."""

    image_kind: Literal["style_image"]
    candidate_id: UUID
    identity: str
    content_sha256: str
    mime_type: str
    width: int
    height: int
    customer_approved: Literal[True]
    publishable: Literal[False]
    source_product_ids: list[str]
    source_asset_ids: list[UUID]


type ProductImageViewItem = Annotated[
    RealPhotoImageReferenceResponse | StyleImageProductViewResponse,
    Field(discriminator="image_kind"),
]


class ProductImageViewResponse(BaseModel):
    """Resolved mall-default or explicit real-photo view for one product."""

    product_id: str
    requested_view: Literal["default", "real"]
    resolved_view: Literal["style_image", "real_photo"]
    images: list[ProductImageViewItem]


type ImageOutputStatusResponse = Annotated[
    RealPhotoOutputResponse | ImageGenerationOutputResponse,
    Field(discriminator="output_type"),
]


class StyleSampleAuthorizeRequest(BaseModel):
    """An explicit consent record for one customer photo."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    photo_id: str = Field(min_length=1, max_length=255)
    consent_text_version: str = Field(min_length=1, max_length=100)


class StyleSampleContentHeaders(BaseModel):
    """Validated metadata for raw historical sample bytes, never a file path."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    photo_id: ProductAssetOpaqueId
    claimed_sha256: Sha256Hex
    mime_type: Literal["image/jpeg", "image/png"]
    consent_granted: Literal["true"]
    consent_provenance: Annotated[str, Field(min_length=1, max_length=100)]
    consent_text_version: Annotated[str, Field(min_length=1, max_length=100)]

    @field_validator("photo_id")
    @classmethod
    def reject_paths(cls, value: str) -> str:
        lowered = value.lower()
        if (
            "/" in value
            or "\\" in value
            or "://" in lowered
            or lowered.startswith("data:")
        ):
            raise ValueError("photo identity must not contain paths or media")
        return value


class StyleSampleResponse(BaseModel):
    """The customer-visible state of an authorized style sample."""

    id: UUID
    photo_id: str
    status: str
    authorized_at: datetime
    withdrawn_at: datetime | None


class StyleSampleContentResponse(StyleSampleResponse):
    """Safe identifier and decoded media facts for a sealed historical sample."""

    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png"]
    width: int
    height: int


class SampleReadinessResponse(BaseModel):
    """Whether enough active authorized samples exist to build a style card."""

    active_sample_count: int
    samples_needed: int
    stability: str
    can_generate: bool


class _StyleFieldOverrides(BaseModel):
    """Validated style fields that a customer may explicitly override."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tone: StyleFieldValues | None = None
    palette: StyleFieldValues | None = None
    lighting: StyleFieldValues | None = None
    composition: StyleFieldValues | None = None
    backgrounds: StyleFieldValues | None = None
    people_and_props: StyleFieldValues | None = None
    material_treatment: StyleFieldValues | None = None
    typography: StyleFieldValues | None = None
    forbidden_elements: StyleFieldValues | None = None


class MainStyleCardEditRequest(_StyleFieldOverrides):
    """Customer edits that create a new immutable main-card version."""

    @model_validator(mode="after")
    def require_at_least_one_edit(self) -> "MainStyleCardEditRequest":
        """Reject no-op PATCH requests before they create meaningless versions."""
        if not self.model_dump(exclude_none=True):
            raise ValueError("at least one style field is required")
        return self


class SceneStyleCardCreateRequest(_StyleFieldOverrides):
    """A named scene plus only the fields that differ from its main card."""

    name: str = Field(min_length=1, max_length=255)
    aspect_ratio: SceneAspectRatio | None = None
    purpose: StyleFieldEntry | None = None
    usage_rules: StyleFieldValues | None = None


class StyleSuitePilotScope(BaseModel):
    """One immutable style-version and slot pair for a controlled pilot."""

    scene_style_version_id: UUID
    slot_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class StyleReferenceResponse(BaseModel):
    """One sealed provider-facing style reference without storage details."""

    model_config = ConfigDict(frozen=True, from_attributes=True)
    sample_id: UUID
    content_sha256: Sha256Hex
    mime_type: Literal["image/jpeg", "image/png"]


class StyleReferencePackResponse(BaseModel):
    """One immutable provider-sized reference selection."""

    model_config = ConfigDict(frozen=True, from_attributes=True)
    id: UUID
    customer_id: UUID
    scene_style_version_id: UUID
    selection_version: str
    manifest_sha256: Sha256Hex
    confirmed_by: str
    active: bool
    created_at: datetime
    references: tuple[StyleReferenceResponse, ...] = Field(min_length=2, max_length=4)


class StyleSuiteBatchStyleSelection(BaseModel):
    """One selected real style and up to two requested suite positions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scene_style_version_id: UUID
    slot_keys: tuple[str, ...] = Field(min_length=1, max_length=2)


class StyleSuiteBatchPreviewRequest(BaseModel):
    """Read-only scope for a first-round real multi-style quote."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: ProductAssetOpaqueId
    source_asset_id: UUID
    product_identity_card_version_id: UUID
    product_identity_observation_run_id: UUID | None = None
    product_identity_observation_review_id: UUID | None = None
    styles: tuple[StyleSuiteBatchStyleSelection, ...] = Field(
        min_length=1, max_length=3
    )


class StyleSuiteBatchPreviewItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    scene_style_version_id: UUID
    style_name: str
    slot_key: str
    style_reference_pack_id: UUID
    style_reference_manifest_sha256: Sha256Hex
    style_reference_count: int = Field(ge=2, le=4)
    style_reference_thumbnail_urls: tuple[str, ...] = Field(min_length=2, max_length=4)
    item_estimated_usd: Decimal = Field(gt=0)
    render_mode: Literal["generative_candidate_only", "pixel_preserving_isolated"] = (
        "generative_candidate_only"
    )
    foreground_version_id: UUID | None = None
    pixel_preserving_eligible: bool = False
    blocking_code: str | None = None
    provider_image_count: int = Field(default=0, ge=0, le=5)
    background_candidate_count: int = Field(default=0, ge=0, le=1)


class StyleSuiteBatchPreviewResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    preview_token: str
    candidate_count: int = Field(ge=1, le=6)
    maximum_estimated_cost_usd: Decimal = Field(gt=0, le=Decimal("3.00"))
    currency: Literal["USD"] = "USD"
    publishable: Literal[False] = False
    items: tuple[StyleSuiteBatchPreviewItem, ...]


class StyleSuiteBatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    preview_token: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    confirmed_maximum_usd: Decimal = Field(gt=0, le=Decimal("3.00"))
    external_processing_confirmed: Literal[True]


class StyleSuitePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: ProductAssetOpaqueId
    source_asset_id: UUID


class StyleSuiteCreateRequest(StyleSuitePreviewRequest):
    idempotency_key: str = Field(min_length=1, max_length=255)


class LocalRealStylePilotRequest(BaseModel):
    """Caller-selected facts plus the exact server-side pilot cost confirmation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_id: str = Field(min_length=1, max_length=128)
    source_asset_id: UUID
    product_identity_card_version_id: UUID
    product_identity_observation_run_id: UUID | None = None
    product_identity_observation_review_id: UUID | None = None
    scene_style_version_id: UUID
    slot_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    # Zero must reach the authority gate so it receives the stable 409 contract
    # rather than FastAPI's generic validation response.
    confirmed_max_usd: Decimal = Field(ge=0, le=Decimal("3.00"))


class LocalRealStyleAnalysisRequest(BaseModel):
    """Exact user-confirmed scope for one paid historical-style analysis."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: str = Field(min_length=1, max_length=255)
    confirmed_maximum_usd: Decimal = Field(ge=0, le=Decimal("1.00"))
    confirmed_reference_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _normalized_identity_card(value: object) -> dict[str, object]:
    """Reuse the domain vocabulary for untrusted request and response cards."""
    try:
        if not isinstance(value, Mapping):
            raise TypeError("identity card must be an object")
        return identity_card_payload(normalize_identity_draft(value))
    except (DomainRuleError, TypeError) as error:
        raise ValueError("product_identity_card_incomplete") from error


class ProductIdentityAnalysisRequest(BaseModel):
    """Exact customer authority for one bounded identity analysis."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=255)
    confirmed_maximum_usd: Decimal = Field(ge=0)
    confirmed_manifest_sha256: Sha256Hex


class ProductIdentityManualDraftRequest(BaseModel):
    """Create one local-only blank draft without provider access or cost."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=255)


class ProductIdentityCardConfirmationRequest(BaseModel):
    """One complete customer-edited card tied to its proposal run."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    analysis_run_id: UUID
    card: dict[str, object]
    confirmed: StrictBool

    @field_validator("card", mode="before")
    @classmethod
    def normalize_card(cls, value: object) -> dict[str, object]:
        return _normalized_identity_card(value)


class ProductIdentityPreviewResponse(BaseModel):
    """Sanitized one-image quote; it grants no provider authority."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    asset_id: UUID
    product_id: str
    source_content_sha256: Sha256Hex
    model: str
    maximum_usd: Decimal = Field(ge=0)
    currency: Literal["USD"]
    input_image_count: Literal[1]
    manifest_sha256: Sha256Hex
    publishable: Literal[False]
    reconciliation_required: bool


class ProductIdentityAnalysisResponse(BaseModel):
    """Sanitized proposal without provider payloads or local storage facts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: UUID
    proposal: dict[str, object]
    created: StrictBool

    @field_validator("proposal", mode="before")
    @classmethod
    def normalize_proposal(cls, value: object) -> dict[str, object]:
        return _normalized_identity_card(value)


class ProductIdentityCardVersionResponse(BaseModel):
    """One immutable customer-confirmed identity-card version."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: UUID
    version: int = Field(gt=0)
    card_sha256: Sha256Hex
    card: dict[str, object]
    created: StrictBool

    @field_validator("card", mode="before")
    @classmethod
    def normalize_card(cls, value: object) -> dict[str, object]:
        return _normalized_identity_card(value)


class ProductIdentityObservationPreviewRequest(BaseModel):
    """Select one confirmed reusable identity card without provider controls."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    card_version_id: UUID


class ProductIdentityObservationExecuteRequest(BaseModel):
    """Exact customer authority for one bounded new-photo observation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    card_version_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=255)
    confirmed_maximum_usd: Decimal = Field(ge=0)
    confirmed_manifest_sha256: Sha256Hex


class ProductIdentityObservationPreviewResponse(BaseModel):
    """Sanitized quote for comparing one new photo with one identity card."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    asset_id: UUID
    product_id: str = Field(min_length=1, max_length=255)
    source_content_sha256: Sha256Hex
    product_identity_card_version_id: UUID
    product_identity_card_sha256: Sha256Hex
    model: str = Field(min_length=1, max_length=100)
    observer_version: str = Field(min_length=1, max_length=100)
    policy_version: str = Field(min_length=1, max_length=100)
    maximum_usd: Decimal = Field(ge=0)
    currency: Literal["USD"]
    input_image_count: Literal[0, 1]
    manifest_sha256: Sha256Hex
    publishable: Literal[False]


class ProductIdentityObservationResponse(BaseModel):
    """One sealed provider observation plus local deterministic decision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: UUID
    observation: dict[str, object]
    decision: Literal["pass", "needs_review", "block"]
    reason_codes: list[str] = Field(max_length=16)
    result_sha256: Sha256Hex
    created: StrictBool

    @field_validator("observation", mode="before")
    @classmethod
    def normalize_observation(cls, value: object) -> dict[str, object]:
        return _normalized_identity_card(value)


class ProductIdentityObservationReviewRequest(BaseModel):
    """Append one explicit human ruling without mutating identity evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=500)
    confirmed: StrictBool


class ProductIdentityObservationReviewResponse(BaseModel):
    """Sanitized append-only human review result."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    review_id: UUID
    observation_run_id: UUID
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=500)
    created: StrictBool


class ProductForegroundDecisionRequest(BaseModel):
    """Exact human decision over one immutable foreground preview."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version_id: UUID
    manifest_sha256: Sha256Hex
    confirmation_key: ImageOutputIdempotencyKey
    decision: Literal["confirmed", "rejected"]
    confirmed: StrictBool


class ProductForegroundEditForm(BaseModel):
    """Strict multipart metadata for one local human mask correction."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_version_id: UUID
    base_manifest_sha256: Sha256Hex
    edit_manifest_sha256: Sha256Hex
    editor_version: Literal["human-mask-editor-v1"]
    idempotency_key: ImageOutputIdempotencyKey
    magic_wand_count: int = Field(ge=0, le=1_000_000)
    erase_stroke_count: int = Field(ge=0, le=1_000_000)
    restore_stroke_count: int = Field(ge=0, le=1_000_000)

    @field_validator(
        "magic_wand_count",
        "erase_stroke_count",
        "restore_stroke_count",
        mode="before",
    )
    @classmethod
    def require_decimal_operation_count(cls, value: object) -> object:
        if not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
            raise ValueError("operation count must be a canonical decimal")
        return value


class ProductForegroundVersionResponse(BaseModel):
    """Sanitized foreground evidence with private byte endpoints only."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: UUID
    asset_id: UUID
    product_id: str
    version: int = Field(gt=0)
    source_content_sha256: Sha256Hex
    foreground_sha256: Sha256Hex
    mask_sha256: Sha256Hex
    width: PositiveImageDimension
    height: PositiveImageDimension
    extractor_version: str
    manifest_sha256: Sha256Hex
    state: Literal["proposed", "confirmed", "rejected"]
    quality_flags: list[str]
    shadow_verdict: Literal["auto_eligible", "human_review_required", "blocked"] | None
    shadow_reason_codes: list[str]
    shadow_model_manifest_sha256: Sha256Hex | None
    pipeline_manifest_sha256: Sha256Hex | None
    stage_receipts_sha256: Sha256Hex | None
    foreground_content_url: str
    mask_content_url: str
    disagreement_content_url: str | None
    disagreement_classification: (
        Literal[
            "no_disagreement",
            "coarse_only_detached_dominant",
            "coarse_only_boundary_dominant",
            "refined_only_dominant",
            "mixed_disagreement",
        ]
        | None
    )
    disagreement_ratio: float | None = Field(default=None, ge=0, le=1)
    coarse_only_ratio: float | None = Field(default=None, ge=0, le=1)
    refined_only_ratio: float | None = Field(default=None, ge=0, le=1)
    coarse_only_detached_ratio: float | None = Field(default=None, ge=0, le=1)
    publishable: Literal[False]
    created: StrictBool


class ForegroundBenchmarkRunRequest(BaseModel):
    """Exact local-only benchmark authority; it carries no photo bytes or paths."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: ImageOutputIdempotencyKey
    dataset_seal_sha256: Sha256Hex
    model_manifest_sha256: Sha256Hex
    config_sha256: Sha256Hex


class ForegroundBenchmarkDatasetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Literal[True]
    dataset_seal_sha256: Sha256Hex
    acceptance_count: int = Field(ge=0)
    calibration_count: int = Field(ge=0)
    categories: list[str]


class ForegroundBenchmarkRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: Literal["running", "completed", "failed"]
    total_samples: int = Field(gt=0)
    completed_samples: int = Field(ge=0)
    failed_samples: int = Field(ge=0)
    dataset_seal_sha256: Sha256Hex
    model_manifest_sha256: Sha256Hex
    config_sha256: Sha256Hex
    failure_code: str | None
    publishable: Literal[False] = False


class ForegroundBenchmarkReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: Literal["PASS", "FAIL"]
    sample_count: int = Field(ge=0)
    auto_eligible_count: int = Field(ge=0)
    human_review_required_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    mean_iou: float = Field(ge=0, le=1)
    mean_background_contamination: float = Field(ge=0, le=1)
    minimum_critical_component_preservation: float = Field(ge=0, le=1)
    maximum_auto_eligible_contamination: float = Field(ge=0, le=1)
    bad_auto_eligible_count: int = Field(ge=0)
    total_elapsed_ms: int = Field(ge=0)
    hardest_sample_ids: list[str] = Field(max_length=10)
    category_breakdown: dict[str, dict[str, int]]
    dataset_seal_sha256: Sha256Hex
    model_manifest_sha256: Sha256Hex
    config_sha256: Sha256Hex
    publishable: Literal[False] = False


class PixelBackgroundPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style_suite_run_id: UUID
    style_suite_run_item_id: UUID
    foreground_version_id: UUID


class PixelBackgroundPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_token: str
    style_suite_run_id: UUID
    style_suite_run_item_id: UUID
    foreground_version_id: UUID
    style_reference_pack_id: UUID
    authorized_manifest_sha256: Sha256Hex
    model: str
    maximum_usd: Decimal = Field(gt=0)
    provider_image_count: int = Field(ge=2, le=4)
    publishable: Literal[False] = False


class PixelBackgroundExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    preview_token: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    confirmed_maximum_usd: Decimal = Field(gt=0, le=Decimal("3.00"))


class PixelBackgroundDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["confirmed", "rejected"]


class PixelBackgroundComposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    translate_x: int = 0
    translate_y: int = 0
    uniform_scale: Decimal = Field(default=Decimal(1), gt=0, le=Decimal(4))
    rotation_degrees: Decimal = Field(default=Decimal(0), ge=-5, le=5)
    output_width: PositiveImageDimension = 1024
    output_height: PositiveImageDimension = 1024


class PixelBackgroundRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    dispatch_state: Literal["reserved", "dispatch_uncertain", "provider_responded"]
    outbound_attempts: int = Field(ge=0, le=1)
    result_sha256: Sha256Hex | None = None
    review_state: Literal["awaiting_review", "confirmed", "rejected"]
    failure_code: str | None = None
    output_width: PositiveImageDimension
    output_height: PositiveImageDimension
    created: StrictBool
    publishable: Literal[False] = False


class PixelCompositionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID
    receipt_id: UUID
    result_sha256: Sha256Hex
    publishable: Literal[False] = False


class PixelCompositionRecommendationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    translate_x: int
    translate_y: int
    uniform_scale: Decimal = Field(gt=0, le=Decimal(4))
    rotation_degrees: Decimal = Field(ge=-5, le=5)
    output_width: PositiveImageDimension
    output_height: PositiveImageDimension
    horizon_y: int = Field(ge=0)
    tabletop_depth_ratio: Decimal = Field(ge=0, le=1)
    product_category: str
    category_profile: str
    category_calibration_state: Literal[
        "calibrated", "pilot_calibrated", "legacy_pilot", "required"
    ]
    placement_state: Literal["ready", "background_unsuitable"]
    save_allowed: StrictBool
    reason_codes: list[str]
    publishable: Literal[False] = False


class LocalRealFidelityRequest(BaseModel):
    """Exact user-confirmed scope for one paid visual fidelity batch."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: str = Field(min_length=1, max_length=255)
    confirmed_maximum_usd: Decimal = Field(ge=0, le=Decimal("1.00"))
    confirmed_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StyleSuitePlanItem(BaseModel):
    """Detached exact revision and slot facts for one future child job."""

    model_config = ConfigDict(frozen=True, from_attributes=True)
    scene_style_id: UUID
    scene_style_version_id: UUID
    style_name: str
    style_version: int
    slot_id: UUID
    slot_key: str
    style_snapshot: dict[str, Any]
    slot_snapshot: dict[str, Any]


class StyleSuitePreviewResponse(BaseModel):
    """Read-only estimate: these facts do not authorize paid generation."""

    model_config = ConfigDict(frozen=True, from_attributes=True)
    customer_id: UUID
    product_id: str
    source_asset_id: UUID
    source_content_sha256: str
    style_count: int
    slot_count: int
    child_job_count: int
    candidate_count: int
    candidate_estimate_usd: Decimal
    maximum_estimated_cost_usd: Decimal
    cost_basis: Literal["local_planning_estimate"] = "local_planning_estimate"
    currency: Literal["USD"] = "USD"
    real_generation_enabled: bool
    items: tuple[StyleSuitePlanItem, ...]


class StyleSuiteExecutionItemResponse(StyleSuitePlanItem):
    id: UUID
    state: str
    output_job_id: UUID | None
    candidate_count: int
    failure_reason: str | None
    fidelity_state: Literal["green", "yellow", "red", "unknown"] | None = None
    fidelity_flags: tuple[str, ...] = ()
    selected_candidate_id: UUID | None = None


class StyleSuiteExecutionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    customer_id: UUID
    product_id: str
    source_asset_id: UUID
    source_content_sha256: str
    product_identity_card_version_id: UUID | None
    product_identity_card_sha256: str | None
    product_identity_observation_run_id: UUID | None
    product_identity_observation_result_sha256: str | None
    product_identity_observation_review_id: UUID | None
    idempotency_key: str
    state: str
    style_count: int
    slot_count: int
    child_job_count: int
    candidate_count: int
    successful_child_count: int
    failed_child_count: int
    uncertain_child_count: int
    pending_child_count: int
    publishable: Literal[False] = False
    items: tuple[StyleSuiteExecutionItemResponse, ...]


class LocalStyleSuiteHistoryResponse(BaseModel):
    """Safe summary produced by the local synthetic history action."""

    sample_count: int
    style_count: int
    slot_count: int
    analysis_adapter: Literal["customer_confirmed_synthetic_history"]
    publishable: Literal[False] = False


class LocalDemoSlotDescriptor(BaseModel):
    """Canonical label and stable key for one server-owned demo position."""

    slot_key: str
    label: str


class LocalDemoStyleDescriptor(BaseModel):
    """Canonical server-owned demo style and its available positions."""

    name: str
    slots: tuple[LocalDemoSlotDescriptor, ...]


class LocalDemoBootstrapResponse(LocalStyleSuiteHistoryResponse):
    """Isolated demo bootstrap facts rendered by the local browser page."""

    styles: tuple[LocalDemoStyleDescriptor, ...]


class LocalDemoRunRequest(BaseModel):
    """Closed zero-network demo request; all generation inputs are server-owned."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: str = Field(min_length=1, max_length=255)


class LocalStyleSuiteReviewCard(BaseModel):
    """One browser review card backed by a persisted candidate row."""

    job_id: UUID
    candidate_id: UUID
    preview_url: str
    source_preview_url: str
    style_name: str
    slot_key: str
    sequence_number: int
    candidate_state: str
    fidelity_state: Literal["green", "yellow", "red", "unknown"] | None
    fidelity_flags: tuple[str, ...]
    fidelity_summary_zh: str | None = None
    selected: bool
    publishable: Literal[False] = False


class LocalStyleSuiteRunResponse(BaseModel):
    """Browser workspace projection over durable suite and candidate state."""

    run_id: UUID
    created_at: datetime
    workspaces: tuple[Literal["historical_styles", "generate_suite"], ...]
    state: str
    progress: dict[str, int]
    review_cards: tuple[LocalStyleSuiteReviewCard, ...]
    generation_adapter: str
    generation_mode: Literal[
        "synthetic_local_deterministic",
        "real_model_local_pilot",
        "pixel_preserving_isolated",
    ]
    outbound_attempts: int = Field(ge=0)
    identity_card_version: int | None = Field(default=None, gt=0)
    identity_card_summary_zh: str | None = None
    publishable: Literal[False] = False


class LocalStyleSuiteBatchCell(BaseModel):
    """One durable style/slot cell in the first-round batch matrix."""

    item_id: UUID
    slot_key: str
    state: str
    render_mode: Literal["generative_candidate_only", "pixel_preserving_isolated"]
    failure_reason: str | None = None
    source_preview_url: str
    style_reference_thumbnail_urls: tuple[str, ...] = Field(min_length=2, max_length=4)
    provider_product_attachment_count: int = Field(default=1, ge=0, le=1)
    provider_style_reference_attachment_count: int = Field(ge=2, le=4)
    candidates: tuple[LocalStyleSuiteReviewCard, ...]
    publishable: Literal[False] = False


class LocalStyleSuiteBatchRow(BaseModel):
    """One immutable real style and its selected suite positions."""

    scene_style_version_id: UUID
    style_name: str
    style_version: int = Field(gt=0)
    style_reference_manifest_sha256: Sha256Hex
    style_reference_thumbnail_urls: tuple[str, ...] = Field(min_length=2, max_length=4)
    slots: tuple[LocalStyleSuiteBatchCell, ...] = Field(min_length=1, max_length=2)


class LocalStyleSuiteBatchRunResponse(BaseModel):
    """Refresh-safe matrix projection over one persisted real batch."""

    id: UUID
    state: str
    matrix: tuple[LocalStyleSuiteBatchRow, ...] = Field(min_length=1, max_length=3)
    candidate_count: int = Field(ge=0, le=6)
    publishable: Literal[False] = False


class StyleTemplateSlotRules(BaseModel):
    """Customer-reviewed directions for one position in a suite."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    aspect_ratio: SceneAspectRatio
    shot_distance: StyleFieldEntry
    camera_angle: StyleFieldEntry
    composition: StyleFieldValues = Field(default_factory=list)
    background: StyleFieldValues = Field(default_factory=list)
    lighting: StyleFieldValues = Field(default_factory=list)
    props: StyleFieldValues = Field(default_factory=list)
    negative_rules: StyleFieldValues = Field(default_factory=list)


class StyleTemplateSlotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    slot_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=255)
    rules: StyleTemplateSlotRules
    active: StrictBool = True


def _validate_slots(slots: list[StyleTemplateSlotRequest]) -> None:
    if len({slot.slot_key for slot in slots}) != len(slots):
        raise ValueError("suite slot keys must be unique")
    if not any(slot.active for slot in slots):
        raise ValueError("at least one suite slot must be active")


class HistoricalStyleGroupConfirmRequest(_StyleFieldOverrides):
    name: str = Field(min_length=1, max_length=255)
    proposal_keys: list[StyleFieldEntry] = Field(default_factory=list, max_length=20)
    evidence_sample_ids: list[UUID] = Field(min_length=1, max_length=100)
    library_sample_count: int | None = Field(default=None, ge=1, le=100)
    analysis_mode: str | None = Field(default=None, min_length=1, max_length=100)
    analyzer_version: str | None = Field(default=None, min_length=1, max_length=100)
    proposal_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reference_selection_version: str | None = Field(
        default=None, min_length=1, max_length=100
    )
    reference_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    field_evidence: dict[str, list[UUID]] = Field(default_factory=dict, max_length=9)
    slots: list[StyleTemplateSlotRequest] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_choices(self) -> "HistoricalStyleGroupConfirmRequest":
        if len(set(self.evidence_sample_ids)) != len(self.evidence_sample_ids):
            raise ValueError("evidence sample ids must be unique")
        _validate_slots(self.slots)
        allowed_fields = set(STYLE_ANALYSIS_LIST_FIELDS)
        if not set(self.field_evidence) <= allowed_fields:
            raise ValueError("field evidence contains an unknown style field")
        evidence_ids = set(self.evidence_sample_ids)
        if any(
            not sample_ids
            or len(set(sample_ids)) != len(sample_ids)
            or not set(sample_ids) <= evidence_ids
            for sample_ids in self.field_evidence.values()
        ):
            raise ValueError("field evidence must reference unique group samples")
        analysis_facts = (
            self.analysis_mode,
            self.analyzer_version,
            self.proposal_fingerprint,
        )
        if any(value is not None for value in analysis_facts):
            if any(value is None for value in analysis_facts):
                raise ValueError("visual analysis facts must be complete")
            populated_fields = {
                field
                for field in STYLE_ANALYSIS_LIST_FIELDS
                if getattr(self, field) is not None and getattr(self, field)
            }
            if set(self.field_evidence) != populated_fields:
                raise ValueError("every reviewed style field requires evidence")
            if self.analysis_mode and "real" in self.analysis_mode:
                if len(self.evidence_sample_ids) < 2:
                    raise ValueError(
                        "real visual style requires at least 2 group samples"
                    )
                if (self.library_sample_count or len(self.evidence_sample_ids)) < 10:
                    raise ValueError(
                        "real visual analysis requires at least 10 samples"
                    )
                for required in ("palette", "lighting", "composition", "backgrounds"):
                    if not getattr(self, required):
                        raise ValueError(f"real visual style requires {required}")
                for slot in self.slots:
                    if slot.active and (
                        not slot.rules.composition
                        or not slot.rules.background
                        or not slot.rules.lighting
                    ):
                        raise ValueError(
                            "real visual style slots require composition background and lighting"
                        )
        selection_facts = (
            self.reference_selection_version,
            self.reference_manifest_sha256,
        )
        if any(value is not None for value in selection_facts) and any(
            value is None for value in selection_facts
        ):
            raise ValueError("reference selection facts must be complete")
        return self


class HistoricalStyleGroupsConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    confirmation_key: str = Field(min_length=1, max_length=100)
    confirmed: StrictBool
    groups: list[HistoricalStyleGroupConfirmRequest] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def require_confirmation(self) -> "HistoricalStyleGroupsConfirmRequest":
        if not self.confirmed:
            raise ValueError("explicit customer confirmation is required")
        if len({group.name for group in self.groups}) != len(self.groups):
            raise ValueError("confirmed group names must be unique")
        return self


class StyleTemplateSlotResponse(StyleTemplateSlotRequest):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: UUID
    customer_id: UUID
    scene_style_id: UUID
    style_card_version_id: UUID
    position: int


class SceneStyleCardEditRequest(_StyleFieldOverrides):
    """Scene changes that append a revision without moving its main-card pin."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    aspect_ratio: SceneAspectRatio | None = None
    purpose: StyleFieldEntry | None = None
    usage_rules: StyleFieldValues | None = None
    slots: list[StyleTemplateSlotRequest] | None = Field(
        default=None, min_length=1, max_length=20
    )

    @model_validator(mode="after")
    def require_at_least_one_edit(self) -> "SceneStyleCardEditRequest":
        if not self.model_fields_set:
            raise ValueError("at least one scene field is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("scene name cannot be cleared")
        if "slots" in self.model_fields_set:
            if self.slots is None:
                raise ValueError("suite slots cannot be cleared")
            _validate_slots(self.slots)
        return self


class MainStyleCardResponse(BaseModel):
    """Latest customer-visible version of the active main style card."""

    id: UUID
    version_id: UUID
    version: int
    kind: str
    status: str
    source: str
    analyzer: str
    analysis_mode: str
    stability: str
    sample_count: int
    tone: list[str]
    palette: list[str]
    lighting: list[str]
    composition: list[str]
    backgrounds: list[str]
    people_and_props: list[str]
    material_treatment: list[str]
    typography: list[str]
    forbidden_elements: list[str]
    evidence_photo_ids: list[str]
    created_at: datetime


class SceneStyleCardResponse(BaseModel):
    """Resolved scene fields backed by explicit overrides and a pinned main version."""

    id: UUID
    version_id: UUID
    version: int
    kind: str
    status: str
    name: str
    aspect_ratio: SceneAspectRatio | None = None
    purpose: StyleFieldEntry | None = None
    usage_rules: list[StyleFieldEntry] = Field(
        default_factory=list,
        max_length=STYLE_FIELD_MAX_ITEMS,
    )
    main_style_card_version_id: UUID | None
    source: str | None = None
    slots: list[StyleTemplateSlotResponse] = Field(default_factory=list)
    overrides: dict[str, list[str]]
    analyzer: str
    analysis_mode: str
    stability: str
    sample_count: int
    tone: list[str]
    palette: list[str]
    lighting: list[str]
    composition: list[str]
    backgrounds: list[str]
    people_and_props: list[str]
    material_treatment: list[str]
    typography: list[str]
    forbidden_elements: list[str]
    evidence_photo_ids: list[str]
    created_at: datetime


class WorkbenchSampleResponse(BaseModel):
    """Path-free browser view of one private benchmark sample."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str
    source_sha256: Sha256Hex
    width: PositiveImageDimension
    height: PositiveImageDimension
    mime_type: Literal["image/jpeg", "image/png"]
    split: Literal["calibration", "acceptance"]
    state: Literal[
        "uploaded", "labeled", "mask_proposed", "human_reviewed", "frozen", "revoked"
    ]
    category: str | None
    difficulty: Literal["easy", "medium", "hard"] | None
    scene_tags: list[str]
    material_tags: list[str]
    critical_component_names: list[str]
    has_proposed_mask: bool
    has_coarse_mask: bool
    has_gold_mask: bool
    proposal_extractor_version: str | None
    proposal_coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    proposal_border_contact_ratio: float | None = Field(default=None, ge=0, le=1)
    proposal_review_risk: Literal["carrier_included"] | None = None
    gold_mask_coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    gold_mask_border_contact_ratio: float | None = Field(default=None, ge=0, le=1)
    gold_mask_soft_edge_ratio: float | None = Field(default=None, ge=0, le=1)
    gold_mask_component_count: int | None = Field(default=None, ge=0)
    gold_mask_review_risks: list[
        Literal["border_contact", "soft_edges", "fragmented_fine_structure"]
    ] = Field(default_factory=list)
    gold_mask_sha256: Sha256Hex | None
    component_mask_sha256s: dict[str, Sha256Hex]
    human_reviewed: bool
    suitability_status: Literal["legacy_eligible", "pending", "eligible", "excluded"]
    suitability_reason: (
        Literal[
            "multi_product_scene",
            "no_unique_product_identity",
            "obstructed_by_tags",
            "other",
        ]
        | None
    )
    suitability_reviewer_scope: str | None
    suitability_reviewed_at: str | None
    suitability_draft_revision: int | None
    review_disposition: Literal["pending", "candidate_usable", "needs_correction"]
    review_disposition_reviewer_scope: str | None
    review_disposition_recorded_at: str | None
    review_disposition_draft_revision: int | None


class WorkbenchProposalDisagreementResponse(BaseModel):
    """Read-only disagreement between coarse and refined machine candidates."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str
    intersection_over_union: float = Field(ge=0, le=1)
    disagreement_ratio: float = Field(ge=0, le=1)
    coarse_only_ratio: float = Field(ge=0, le=1)
    refined_only_ratio: float = Field(ge=0, le=1)
    coarse_polarity: Literal[
        "foreground", "background_inverted_for_comparison"
    ]
    raw_coarse_coverage_ratio: float = Field(ge=0, le=1)
    raw_coarse_border_contact_ratio: float = Field(ge=0, le=1)


class WorkbenchReviewAuditResponse(BaseModel):
    """Dataset revision plus machine-only review ordering evidence."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    revision: int = Field(ge=0)
    items: list[WorkbenchProposalDisagreementResponse]


class WorkbenchBlockerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    missing_count: int = Field(ge=0)


class WorkbenchPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pilot_ready: bool
    pilot_blockers: list[WorkbenchBlockerResponse]
    ready_to_freeze: bool
    calibration_count: int = Field(ge=0)
    acceptance_count: int = Field(ge=0)
    unlabeled_count: int = Field(ge=0)
    unreviewed_count: int = Field(ge=0)
    unauthorized_count: int = Field(ge=0)
    duplicate_source_count: int = Field(ge=0)
    suitability_pending_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    blockers: list[WorkbenchBlockerResponse]


class WorkbenchDatasetResponse(BaseModel):
    """Customer-safe draft response; storage facts are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    revision: int = Field(ge=0)
    state: Literal["draft", "frozen", "revoked"]
    samples: list[WorkbenchSampleResponse]
    preflight: WorkbenchPreflightResponse


class WorkbenchLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=0)
    category: str = Field(min_length=1, max_length=100)
    difficulty: Literal["easy", "medium", "hard"]
    scene_tags: list[str] = Field(default_factory=list, max_length=20)
    material_tags: list[str] = Field(default_factory=list, max_length=20)
    critical_component_names: list[str] = Field(max_length=20)


class WorkbenchComponentAnnotationReopenRequest(BaseModel):
    """Explicitly invalidate a prior sign-off before adding component masks."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=0)
    critical_component_names: list[str] = Field(max_length=20)
    confirmed: bool


class WorkbenchSuitabilityRequest(BaseModel):
    """Explicit human gate for whether a sample may count toward acceptance."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=0)
    decision: Literal["pending", "eligible", "excluded"]
    reason: (
        Literal[
            "multi_product_scene",
            "no_unique_product_identity",
            "obstructed_by_tags",
            "other",
        ]
        | None
    ) = None
    confirmed: bool


class WorkbenchReviewDispositionRequest(BaseModel):
    """Human queue triage that never promotes a candidate to gold."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    disposition: Literal["pending", "candidate_usable", "needs_correction"]
    confirmed: bool


type NormalizedCoordinate = Annotated[float, Field(ge=0.0, le=1.0)]
type NormalizedPoint = tuple[NormalizedCoordinate, NormalizedCoordinate]


class WorkbenchProposalRequest(BaseModel):
    """Optional human placement hints for one local machine-mask proposal."""

    model_config = ConfigDict(extra="forbid")

    box: (
        tuple[
            NormalizedCoordinate,
            NormalizedCoordinate,
            NormalizedCoordinate,
            NormalizedCoordinate,
        ]
        | None
    ) = None
    positive_points: list[NormalizedPoint] = Field(default_factory=list, max_length=20)
    negative_points: list[NormalizedPoint] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_prompt(self) -> "WorkbenchProposalRequest":
        if self.box is not None and (
            self.box[0] >= self.box[2] or self.box[1] >= self.box[3]
        ):
            raise ValueError("foreground_prompt_invalid")
        return self


class WorkbenchPromptSuggestionResponse(BaseModel):
    """Review-only local placement hints; this never confirms a mask."""

    model_config = ConfigDict(extra="forbid")

    source_sha256: Sha256Hex
    box: tuple[
        NormalizedCoordinate,
        NormalizedCoordinate,
        NormalizedCoordinate,
        NormalizedCoordinate,
    ]
    positive_points: list[NormalizedPoint] = Field(max_length=2)
    negative_points: list[NormalizedPoint] = Field(default_factory=list, max_length=20)
    method: Literal["flat-lay-color-components-v1"]
    component_count: int = Field(ge=1, le=2)
    review_required: Literal[True]


class WorkbenchStyleImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    style_sample_id: UUID
    split: Literal["calibration", "acceptance"]


class WorkbenchHumanReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=0)
    confirmed: StrictBool
    source_sha256: Sha256Hex
    gold_mask_sha256: Sha256Hex
    component_mask_sha256s: dict[str, Sha256Hex]
    consent_text_version: str = Field(min_length=1, max_length=100)


class WorkbenchDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: Literal[True]
    revision: int = Field(ge=1)


class WorkbenchFreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=100)


class WorkbenchFreezeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    version_id: UUID
    seal_sha256: Sha256Hex
    acceptance_count: int
    calibration_count: int
    activated: Literal[True]
    benchmark_started: Literal[False]


class WorkbenchRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_sha256: Sha256Hex
    confirmed: StrictBool
