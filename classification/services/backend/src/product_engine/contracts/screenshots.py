from typing import Literal
from uuid import UUID

from pydantic import Field

from product_engine.contracts.common import EventEnvelope, ProductCode, Sha256Hex


class ScreenshotImportedV1(EventEnvelope):
    screenshot_import_id: UUID
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: Sha256Hex
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    byte_size: int = Field(ge=1, le=20_971_520)
    session_id: UUID | None = None
    product_code: ProductCode | None = None
