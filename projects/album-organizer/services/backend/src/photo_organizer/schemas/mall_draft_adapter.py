import json
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from photo_organizer.categorization.contracts import (
    FrozenModel,
    ProductSourceType,
    StableId,
)
from photo_organizer.schemas.acceptance_receipt import (
    AcceptedImageEvidence,
    MaterialDataClassification,
    UnevaluatedClaims,
)
from photo_organizer.schemas.mall_draft_export import (
    BusinessFieldRequirements,
    MallDraftClassification,
)
from photo_organizer.schemas.standard_product_package import (
    BatchId,
    PackageId,
    Sha256Digest,
)

PlanId = Annotated[
    str,
    Field(pattern=r"^mall-draft-import-plan:[0-9a-f]{64}$"),
]
MallDraftItemStatus = Literal[
    "prepared",
    "created",
    "change_proposal_required",
    "retryable_failed",
    "permanently_blocked",
]
MallDraftItemErrorCode = Literal["gateway_unavailable", "gateway_protocol_error"]


class MallDraftImportItem(FrozenModel):
    draft_request_id: StableId
    source_acceptance_id: StableId
    source_group_confirmation_event_id: UUID
    source_album_product_id: StableId
    source_snapshot_version: Annotated[int, Field(ge=1)]
    source_type: ProductSourceType
    classification: MallDraftClassification
    images: Annotated[
        tuple["MallDraftImageEvidence", ...], Field(min_length=2, max_length=4)
    ]
    required_business_fields: BusinessFieldRequirements
    material_authorization: Literal["opc_human_confirmed_for_draft_material_use"]
    claims_not_evaluated: UnevaluatedClaims
    mall_product_id: None
    bead_id: None
    publication_state: Literal["draft"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]

    @field_validator("images", mode="before")
    @classmethod
    def remove_private_storage_references(cls, value: object) -> object:
        if not isinstance(value, tuple):
            return value
        return tuple(
            MallDraftImageEvidence.model_validate(
                image.model_dump(mode="python", exclude={"storage_ref"})
            )
            if isinstance(image, AcceptedImageEvidence)
            else image
            for image in value
        )


class MallDraftImageEvidence(FrozenModel):
    """Public plan evidence, deliberately without any private storage reference."""

    asset_id: UUID
    evidence_asset_id: UUID
    role: Literal["original"]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    byte_size: Annotated[int, Field(gt=0, le=104_857_600)]
    media_type: Literal["image/jpeg", "image/png", "image/heic", "image/heif"]
    captured_at: datetime | None
    imported_at: datetime


class MallDraftImportPlan(FrozenModel):
    schema_version: Literal["opc-mall-draft-import-plan/1.0"]
    plan_id: PlanId
    opc_id: StableId
    source_batch_id: BatchId
    package_id: PackageId
    package_acceptance_id: Annotated[
        str, Field(pattern=r"^standard-package-acceptance:[0-9a-f]{64}$")
    ]
    package_sha256: Sha256Digest
    data_classification: MaterialDataClassification
    operation: Literal["create_draft_only"]
    existing_product_policy: Literal["change_proposal_required"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]
    items: Annotated[
        tuple[MallDraftImportItem, ...], Field(min_length=1, max_length=10)
    ]

    @model_validator(mode="after")
    def plan_id_must_bind_complete_body(self) -> Self:
        if self.plan_id != _plan_id(self.model_dump(mode="json", exclude={"plan_id"})):
            raise ValueError("plan id must bind the complete import plan")
        return self


class MallDraftItemResult(FrozenModel):
    draft_request_id: StableId
    attempt_generation: Annotated[int, Field(ge=1)]
    status: MallDraftItemStatus
    mall_product_id: StableId | None
    error_code: MallDraftItemErrorCode | None

    @model_validator(mode="after")
    def status_requires_exact_identity_and_error_fields(self) -> Self:
        expected = {
            "prepared": (False, None),
            "created": (True, None),
            "change_proposal_required": (False, None),
            "retryable_failed": (False, "gateway_unavailable"),
            "permanently_blocked": (False, "gateway_protocol_error"),
        }[self.status]
        actual = (self.mall_product_id is not None, self.error_code)
        if actual != expected:
            raise ValueError("result status fields are inconsistent")
        return self


class MallDraftImportReceipt(FrozenModel):
    schema_version: Literal["opc-mall-draft-import-receipt/1.0"]
    plan_id: PlanId
    opc_id: StableId
    execution_status: Literal["prepared", "in_progress", "completed"]
    item_count: Annotated[int, Field(ge=1, le=10)]
    results: Annotated[
        tuple[MallDraftItemResult, ...], Field(min_length=1, max_length=10)
    ]

    @model_validator(mode="after")
    def result_set_must_be_complete_and_ordered(self) -> Self:
        result_ids = tuple(result.draft_request_id for result in self.results)
        terminal = {"created", "change_proposal_required", "permanently_blocked"}
        all_terminal = all(result.status in terminal for result in self.results)
        if (
            self.item_count != len(self.results)
            or result_ids != tuple(sorted(result_ids))
            or len(set(result_ids)) != len(result_ids)
            or (self.execution_status == "completed") != all_terminal
        ):
            raise ValueError("import receipt results must be complete and ordered")
        return self


def canonical_plan_json(body: dict[str, object]) -> str:
    return json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _plan_id(body: dict[str, object]) -> str:
    return f"mall-draft-import-plan:{sha256(canonical_plan_json(body).encode()).hexdigest()}"


def plan_id_for_body(body: dict[str, object]) -> str:
    return _plan_id(body)
