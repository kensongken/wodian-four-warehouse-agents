from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from product_engine.contracts.sales import SalesMaterialUsageV1
from product_engine.repositories.events import ClaimResult


@dataclass(frozen=True)
class UsageResult:
    disposition: Literal["accepted", "duplicate"]


class UsageStore(Protocol):
    async def record(self, event: SalesMaterialUsageV1, payload_sha256: str) -> UsageResult: ...


class InboxStore(Protocol):
    async def claim_inbox(
        self,
        tenant_id: UUID,
        source_system: str,
        event_id: UUID,
        payload_sha256: str,
    ) -> ClaimResult: ...


class SalesUsageService:
    """在同一数据库事务中完成销售事件幂等认领与素材使用写入。"""

    def __init__(
        self,
        store: UsageStore | None = None,
        *,
        inbox: InboxStore | None = None,
    ) -> None:
        self.store = store
        self.inbox = inbox

    async def record(self, event: SalesMaterialUsageV1, payload_sha256: str) -> UsageResult:
        if self.store is None or self.inbox is None:
            raise RuntimeError("sales_usage_storage_not_configured")
        claim = await self.inbox.claim_inbox(
            event.tenant_id, "sales", event.event_id, payload_sha256
        )
        if claim.disposition == "duplicate":
            return UsageResult("duplicate")
        return await self.store.record(event, payload_sha256)
