from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from product_engine.contracts.common import EventEnvelope, ProductCode, Sha256Hex


class MaterialAssetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_id: UUID
    upstream_asset_id: UUID
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: Sha256Hex
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    role: Literal["main_candidate", "gallery", "source_original"]


class ProductMaterialsUpdatedV1(EventEnvelope):
    product_code: ProductCode
    package_version: int = Field(ge=1)
    status: Literal["source_ready"]
    source_assets: list[MaterialAssetRef] = Field(min_length=1)
    generated_assets: list[MaterialAssetRef] = Field(default_factory=list, max_length=0)
    content_status: Literal["pending"]
    price_status: Literal["pending"]
