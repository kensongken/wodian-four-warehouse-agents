from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from photo_organizer.categorization.contracts import StableId


class MyOpcResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StableId
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    role: Literal["owner", "reviewer"]


class MyOpcListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MyOpcResponse]
