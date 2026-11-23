"""Strict Mall merchant style-onboarding contracts."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MallStyleOnboardingConfirmationV1(BaseModel):
    """Owner-confirmed facts required before a style becomes selectable."""

    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_name: str = Field(min_length=1, max_length=80)
    evidence_sample_ids: list[UUID] = Field(min_length=10, max_length=100)
    default_image_placement: Literal["primary", "detail"]
    consent_text_version: str = Field(min_length=1, max_length=100)
    owner_actor_reference: str = Field(min_length=1, max_length=255)
    confirmed: Literal[True]

    @field_validator("candidate_name", "consent_text_version", "owner_actor_reference")
    @classmethod
    def no_surrounding_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("surrounding_whitespace_not_allowed")
        return value

    @field_validator("evidence_sample_ids")
    @classmethod
    def unique_evidence(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate_evidence_sample")
        return value


class MallStyleOnboardingSampleReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["mall-style-onboarding-sample-v1"] = (
        "mall-style-onboarding-sample-v1"
    )
    merchant_id: UUID
    opc_id: UUID
    source_asset_id: str
    style_sample_id: UUID
    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png"]
    status: Literal["usable"] = "usable"
    generation_started: Literal[False] = False
    publishable: Literal[False] = False


class MallStyleOnboardingConfirmationReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["mall-style-onboarding-confirmation-v1"] = (
        "mall-style-onboarding-confirmation-v1"
    )
    merchant_id: UUID
    opc_id: UUID
    candidate_id: UUID
    scene_style_id: UUID
    style_card_version_id: UUID
    style_version: int = Field(gt=0)
    default_image_placement: Literal["primary", "detail"]
    generation_started: Literal[False] = False
    publishable: Literal[False] = False
