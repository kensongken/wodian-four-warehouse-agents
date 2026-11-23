from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ProductMaterialDownloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["opc-product-material-download/1.0"]
    confirmation_event_id: UUID
    asset_id: UUID
    download_url: str
    expires_seconds: Literal[300]
