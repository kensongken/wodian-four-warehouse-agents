import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from product_engine.contracts.common import ProductCode, Sha256Hex


class ClassificationAssetConfirmation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: UUID
    position: int = Field(ge=0)
    role: Literal["main_candidate", "gallery", "source_original"]
    sha256: Sha256Hex


class ClassificationConfirmationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    product_code: ProductCode
    local_batch_id: str = Field(min_length=1, max_length=128)
    base_material_package_version: int = Field(ge=1)
    material_label: str = Field(min_length=1, max_length=128)
    product_form: str = Field(min_length=1, max_length=64)
    main_asset_id: UUID
    visual_observations: list[str] = Field(default_factory=list, max_length=100)
    selling_point_candidates: list[str] = Field(default_factory=list, max_length=100)
    inventory_status: Literal["in_stock", "reserved", "out_of_stock"] | None
    price_status: Literal["pending", "human_confirmed"]
    price_candidate: dict[str, Any] | None
    service_rule_summaries: list[str] = Field(default_factory=list, max_length=100)
    review_source: str = Field(min_length=1, max_length=64)
    assets: list[ClassificationAssetConfirmation] = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def validate_confirmation(self) -> "ClassificationConfirmationCommand":
        positions = [asset.position for asset in self.assets]
        if positions != list(range(len(self.assets))):
            raise ValueError("classification_asset_positions_invalid")

        asset_ids = [asset.asset_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("classification_asset_ids_not_unique")

        hashes = [asset.sha256 for asset in self.assets]
        if len(hashes) != len(set(hashes)):
            raise ValueError("classification_asset_hashes_not_unique")

        if self.main_asset_id not in set(asset_ids):
            raise ValueError("classification_main_asset_missing")
        if self.price_status == "pending" and self.price_candidate is not None:
            raise ValueError("pending_price_candidate_must_be_empty")
        if self.price_status == "human_confirmed" and self.price_candidate is None:
            raise ValueError("confirmed_price_candidate_required")
        return self


class ClassificationConfirmationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    product_code: ProductCode
    classification_version: int = Field(ge=1)
    classification_id: UUID
    content_sha256: Sha256Hex
    material_package_version: int = Field(ge=1)
    material_label: str = Field(min_length=1, max_length=128)
    material_evidence: Literal["human_confirmed", "information_insufficient"]
    disposition: Literal["created", "duplicate"]
    downstream_status: Literal["not_sent"] = "not_sent"

    @model_validator(mode="after")
    def validate_material_evidence(self) -> "ClassificationConfirmationReceipt":
        expected = (
            "information_insufficient" if self.material_label == "信息不足" else "human_confirmed"
        )
        if self.material_evidence != expected:
            raise ValueError("classification_material_evidence_mismatch")
        return self


def canonical_classification_sha256(
    command: ClassificationConfirmationCommand,
) -> str:
    body = command.model_dump(
        mode="json",
        exclude={"request_id", "local_batch_id"},
    )
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
