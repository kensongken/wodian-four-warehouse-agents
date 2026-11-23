from importlib import import_module
from uuid import UUID

from product_engine.contracts.album import ProductImageSetReadyV1


class FakeEvents:
    def __init__(self, disposition: str) -> None:
        self.disposition = disposition
        self.outbox = []

    async def claim_inbox(self, tenant_id, source_system, event_id, payload_sha256):
        repositories = import_module("product_engine.repositories.events")
        return repositories.ClaimResult(self.disposition)

    async def append_outbox(self, tenant_id, topic, aggregate_key, payload):
        self.outbox.append((tenant_id, topic, aggregate_key, payload))
        return UUID("99999999-9999-4999-8999-999999999999")


class FakeProducts:
    def __init__(self) -> None:
        self.upsert_count = 0

    async def latest_package_version(self, tenant_id, product_code):
        return 3

    async def upsert_image_set(self, event):
        self.upsert_count += 1
        return 4

    async def build_materials_event(self, tenant_id, product_code, version):
        return {
            "schema_version": 1,
            "event_id": "33333333-3333-4333-8333-333333333333",
            "occurred_at": "2026-08-31T08:01:00Z",
            "tenant_id": str(tenant_id),
            "product_code": product_code,
            "package_version": version,
            "status": "source_ready",
            "source_assets": [
                {
                    "material_id": "55555555-5555-4555-8555-555555555555",
                    "upstream_asset_id": "44444444-4444-4444-8444-444444444444",
                    "object_key": (
                        "tenants/22222222-2222-4222-8222-222222222222/"
                        "products/SP-00831/source/main.heic"
                    ),
                    "sha256": "a" * 64,
                    "mime_type": "image/heic",
                    "role": "main_candidate",
                }
            ],
            "generated_assets": [],
            "content_status": "pending",
            "price_status": "pending",
        }


class FakeStrategies:
    def __init__(self) -> None:
        self.values = []

    async def append_preview(self, value):
        from product_engine.repositories.sales_strategy_inputs import AppendStrategyResult

        self.values.append(value)
        return AppendStrategyResult(value=value, created=True)


def album_event(valid_album_payload) -> ProductImageSetReadyV1:
    return ProductImageSetReadyV1.model_validate(valid_album_payload)


async def test_new_album_event_creates_package_and_outbox(valid_album_payload) -> None:
    module = import_module("product_engine.services.album_ingestion")
    events = FakeEvents("new")
    products = FakeProducts()
    strategies = FakeStrategies()
    service = module.AlbumIngestionService(events, products, strategies)

    result = await service.ingest(album_event(valid_album_payload), "a" * 64)

    assert result.disposition == "accepted"
    assert result.package_version == 4
    assert products.upsert_count == 1
    assert len(events.outbox) == 2
    assert events.outbox[0][1] == "product.materials.updated.v1"
    assert events.outbox[1][1] == "sales.strategy.input.updated.v1"
    assert events.outbox[1][2] == ("22222222-2222-4222-8222-222222222222:SP-00831")
    assert events.outbox[1][3]["status"] == "preview_only"
    assert len(strategies.values) == 1


async def test_duplicate_album_event_has_no_product_or_outbox_write(
    valid_album_payload,
) -> None:
    module = import_module("product_engine.services.album_ingestion")
    events = FakeEvents("duplicate")
    products = FakeProducts()
    strategies = FakeStrategies()
    service = module.AlbumIngestionService(events, products, strategies)

    result = await service.ingest(album_event(valid_album_payload), "a" * 64)

    assert result.disposition == "duplicate"
    assert result.package_version == 3
    assert products.upsert_count == 0
    assert events.outbox == []
    assert strategies.values == []
