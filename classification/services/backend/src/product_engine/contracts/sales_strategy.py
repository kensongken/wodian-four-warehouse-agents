import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from product_engine.contracts.common import EventEnvelope, ProductCode, Sha256Hex


class SalesStrategyImageRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: UUID
    material_id: UUID | None = None
    sha256: Sha256Hex
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    role: Literal["main_candidate", "gallery", "source_original"]


class CandidateProductFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1, max_length=64)
    confirmation_status: Literal["candidate"] = "candidate"


class SalesStrategyInputV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["1-sales-strategy-input"] = "1-sales-strategy-input"
    tenant_id: UUID
    product_code: ProductCode
    strategy_input_version: int = Field(ge=1)
    product_data_version: int = Field(ge=1)
    source_album_event_id: UUID
    source_material_package_version: int = Field(ge=1)
    source_image_set_sha256: Sha256Hex
    images: list[SalesStrategyImageRef] = Field(min_length=1, max_length=3000)
    material: CandidateProductFact
    form: CandidateProductFact
    inventory_status: Literal["pending"] = "pending"
    visual_observations: list[str] = Field(default_factory=list, max_length=100)
    selling_point_candidates: list[str] = Field(default_factory=list, max_length=100)
    price_status: Literal["pending"] = "pending"
    service_rule_summaries: list[str] = Field(default_factory=list, max_length=100)
    unverified_items: list[str] = Field(default_factory=list, max_length=100)
    forbidden_expressions: list[str] = Field(default_factory=list, max_length=100)
    status: Literal["preview_only"] = "preview_only"
    created_at: AwareDatetime
    content_sha256: Sha256Hex
    automatic_customer_contact_allowed: Literal[False] = False
    automatic_publication_allowed: Literal[False] = False
    automatic_wechat_send_allowed: Literal[False] = False
    automatic_quote_allowed: Literal[False] = False


class SalesStrategyInputUpdatedV1(EventEnvelope):
    model_config = ConfigDict(frozen=True, extra="forbid")

    product_code: ProductCode
    strategy_input_version: int = Field(ge=1)
    product_data_version: int = Field(ge=1)
    status: Literal["preview_only"] = "preview_only"
    content_sha256: Sha256Hex
    automatic_customer_contact_allowed: Literal[False] = False
    automatic_publication_allowed: Literal[False] = False
    automatic_wechat_send_allowed: Literal[False] = False
    automatic_quote_allowed: Literal[False] = False


def canonical_sales_strategy_sha256(value: SalesStrategyInputV1) -> str:
    body = value.model_dump(
        mode="json",
        exclude_none=True,
        exclude={
            "content_sha256",
            "strategy_input_version",
            "created_at",
            "source_album_event_id",
        },
    )
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
