from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol
from uuid import UUID

from photo_organizer.categorization.contracts import FrozenModel, StableId
from photo_organizer.schemas.mall_draft_adapter import MallDraftImportItem

MallDraftLookupStatus = Literal[
    "not_found",
    "existing_draft",
    "existing_published",
    "ambiguous",
    "temporarily_unavailable",
]


@dataclass(frozen=True, repr=False)
class DraftImageSource:
    """Opaque metadata plus an executor-enforced one-shot image stream handle."""

    asset_id: UUID
    evidence_asset_id: UUID
    media_type: Literal["image/jpeg", "image/png", "image/heic", "image/heif"]
    byte_size: int
    open: Callable[[], AbstractContextManager[BinaryIO]]


class MallDraftLookupResult(FrozenModel):
    status: MallDraftLookupStatus


class MallDraftCreateResult(FrozenModel):
    opc_id: StableId
    draft_request_id: StableId
    source_album_product_id: StableId
    mall_product_id: StableId
    publication_state: Literal["draft"]


class MallDraftGateway(Protocol):
    async def lookup_by_source_album_product(
        self, *, opc_id: str, source_album_product_id: str
    ) -> MallDraftLookupResult: ...

    async def create_draft(
        self,
        *,
        idempotency_key: str,
        command: MallDraftImportItem,
        image_sources: tuple[DraftImageSource, ...],
    ) -> MallDraftCreateResult: ...
