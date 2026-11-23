from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid4

from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.contracts.materials import ProductMaterialsUpdatedV1
from product_engine.contracts.sales_strategy import (
    SalesStrategyInputUpdatedV1,
    SalesStrategyInputV1,
)
from product_engine.repositories.events import ClaimResult
from product_engine.repositories.sales_strategy_inputs import AppendStrategyResult
from product_engine.services.sales_strategy_inputs import (
    build_preview_sales_strategy_input,
)


class EventStore(Protocol):
    async def claim_inbox(
        self,
        tenant_id: UUID,
        source_system: str,
        event_id: UUID,
        payload_sha256: str,
    ) -> ClaimResult: ...

    async def append_outbox(
        self,
        tenant_id: UUID,
        topic: str,
        aggregate_key: str,
        payload: dict[str, object],
    ) -> UUID: ...


class ProductStore(Protocol):
    async def latest_package_version(self, tenant_id: UUID, product_code: str) -> int: ...

    async def upsert_image_set(self, event: ProductImageSetReadyV1) -> int: ...

    async def build_materials_event(
        self,
        tenant_id: UUID,
        product_code: str,
        version: int,
    ) -> dict[str, object]: ...


class StrategyStore(Protocol):
    async def append_preview(
        self,
        candidate: SalesStrategyInputV1,
    ) -> AppendStrategyResult: ...


@dataclass(frozen=True)
class IngestionResult:
    event_id: UUID
    product_code: str
    package_version: int
    disposition: Literal["accepted", "duplicate"]


class AlbumIngestionService:
    def __init__(
        self,
        events: EventStore,
        products: ProductStore,
        strategies: StrategyStore,
    ) -> None:
        self.events = events
        self.products = products
        self.strategies = strategies

    async def ingest(
        self,
        event: ProductImageSetReadyV1,
        payload_sha256: str,
    ) -> IngestionResult:
        claim = await self.events.claim_inbox(
            event.tenant_id, "album", event.event_id, payload_sha256
        )
        if claim.disposition == "duplicate":
            version = await self.products.latest_package_version(
                event.tenant_id,
                event.product_code,
            )
            return IngestionResult(
                event.event_id,
                event.product_code,
                version,
                "duplicate",
            )

        version = await self.products.upsert_image_set(event)
        payload = await self.products.build_materials_event(
            event.tenant_id,
            event.product_code,
            version,
        )
        await self.events.append_outbox(
            event.tenant_id,
            "product.materials.updated.v1",
            event.product_code,
            payload,
        )
        materials = ProductMaterialsUpdatedV1.model_validate(payload)
        strategy_result = await self.strategies.append_preview(
            build_preview_sales_strategy_input(event, materials)
        )
        if strategy_result.created:
            strategy = strategy_result.value
            update = SalesStrategyInputUpdatedV1(
                schema_version=1,
                event_id=uuid4(),
                occurred_at=strategy.created_at,
                tenant_id=strategy.tenant_id,
                product_code=strategy.product_code,
                strategy_input_version=strategy.strategy_input_version,
                product_data_version=strategy.product_data_version,
                status="preview_only",
                content_sha256=strategy.content_sha256,
            )
            await self.events.append_outbox(
                event.tenant_id,
                "sales.strategy.input.updated.v1",
                f"{event.tenant_id}:{event.product_code}",
                update.model_dump(mode="json"),
            )
        return IngestionResult(
            event.event_id,
            event.product_code,
            version,
            "accepted",
        )
