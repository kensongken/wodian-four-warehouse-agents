import hashlib
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from product_engine.contracts.common import ProductCode, Sha256Hex
from product_engine.contracts.sales_strategy import SalesStrategyImageRef

IdempotencyKey = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=255),
]


class ConfirmedProductFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(min_length=1, max_length=128)
    confirmation_status: Literal["human_confirmed"] = "human_confirmed"
    evidence: Literal["human_confirmed", "information_insufficient"]


class SalesStrategyInputV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["2-sales-strategy-input"] = "2-sales-strategy-input"
    tenant_id: UUID
    product_code: ProductCode
    strategy_input_version: int = Field(ge=1)
    source_material_package_version: int = Field(ge=1)
    source_classification_id: UUID
    source_classification_version: int = Field(ge=1)
    source_classification_sha256: Sha256Hex
    source_image_set_sha256: Sha256Hex
    images: tuple[SalesStrategyImageRef, ...] = Field(min_length=1, max_length=3000)
    material: ConfirmedProductFact
    form: ConfirmedProductFact
    inventory_status: Literal["in_stock", "reserved", "out_of_stock"] | None = None
    visual_observations: tuple[str, ...] = Field(default=(), max_length=100)
    selling_point_candidates: tuple[str, ...] = Field(default=(), max_length=100)
    price_status: Literal["pending", "human_confirmed"] = "pending"
    price_candidate: dict[str, Any] | None = None
    service_rule_summaries: tuple[str, ...] = Field(default=(), max_length=100)
    unverified_items: tuple[str, ...] = Field(default=(), max_length=100)
    forbidden_expressions: tuple[str, ...] = Field(default=(), max_length=100)
    status: Literal["preview_only"] = "preview_only"
    created_at: AwareDatetime
    content_sha256: Sha256Hex
    automatic_customer_contact_allowed: Literal[False] = False
    automatic_publication_allowed: Literal[False] = False
    automatic_wechat_send_allowed: Literal[False] = False
    automatic_quote_allowed: Literal[False] = False

    @field_validator(
        "automatic_customer_contact_allowed",
        "automatic_publication_allowed",
        "automatic_wechat_send_allowed",
        "automatic_quote_allowed",
        mode="before",
    )
    @classmethod
    def require_exact_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("automatic permissions must be false")
        return value

    @model_validator(mode="after")
    def require_material_warning(self) -> "SalesStrategyInputV2":
        if (
            self.material.evidence == "information_insufficient"
            and "材质待人工确认" not in self.unverified_items
        ):
            raise ValueError("material warning is required")
        if self.price_status == "pending" and self.price_candidate is not None:
            raise ValueError("pending price candidate must be empty")
        if self.price_status == "human_confirmed" and self.price_candidate is None:
            raise ValueError("confirmed price candidate is required")
        return self


class SalesWarehouseDeliveryV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["sales-warehouse-delivery-v1"] = "sales-warehouse-delivery-v1"
    handoff_id: UUID
    idempotency_key: IdempotencyKey
    payload_sha256: Sha256Hex
    payload: SalesStrategyInputV2

    @model_validator(mode="after")
    def bind_delivery_to_payload(self) -> "SalesWarehouseDeliveryV1":
        if self.payload_sha256 != self.payload.content_sha256:
            raise ValueError("payload digest must match")
        expected_key = f"sales:{self.payload.tenant_id}:{self.payload.source_classification_id}"
        if self.idempotency_key != expected_key:
            raise ValueError("idempotency key must match payload source")
        return self


class SalesWarehouseReceiptV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["sales-warehouse-receipt-v1"] = "sales-warehouse-receipt-v1"
    receiver_receipt_id: UUID
    handoff_id: UUID
    accepted_payload_sha256: Sha256Hex
    status: Literal["accepted"] = "accepted"
    accepted_at: AwareDatetime


def canonical_sales_strategy_v2_sha256(value: SalesStrategyInputV2) -> str:
    body = value.model_dump(
        mode="json",
        exclude={"content_sha256", "strategy_input_version", "created_at"},
    )
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
