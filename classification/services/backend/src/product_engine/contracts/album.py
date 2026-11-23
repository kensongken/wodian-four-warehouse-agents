from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from product_engine.contracts.common import (
    Confidence,
    EventEnvelope,
    ProductCode,
    Sha256Hex,
)


class SourceImageRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: UUID
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: Sha256Hex
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    kind: Literal["original", "crop"]
    role: Literal["main_candidate", "gallery", "source_original"]
    quality_confidence: Confidence


class ProductImageSetReadyV1(EventEnvelope):
    source_task_id: UUID
    product_code: ProductCode
    category: str = Field(min_length=1, max_length=64)
    form: str = Field(min_length=1, max_length=64)
    classification_confidence: Confidence
    images: list[SourceImageRef] = Field(min_length=1, max_length=3000)
