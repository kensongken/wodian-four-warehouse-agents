from typing import Literal
from uuid import UUID

from pydantic import Field

from product_engine.contracts.common import EventEnvelope, ProductCode


class SalesMaterialUsageV1(EventEnvelope):
    session_id: UUID
    product_code: ProductCode
    package_version: int = Field(ge=1)
    usage_type: Literal["loaded", "copied", "share_initiated"]
    material_ids: list[UUID] = Field(min_length=1, max_length=100)
