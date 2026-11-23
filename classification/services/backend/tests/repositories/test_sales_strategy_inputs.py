from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from product_engine.contracts.sales_strategy import (
    SalesStrategyInputV1,
    canonical_sales_strategy_sha256,
)
from product_engine.repositories.sales_strategy_inputs import (
    SalesStrategyInputRepository,
    latest_strategy_statement,
    strategy_by_version_statement,
    strategy_product_statement,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


def normalized(statement) -> str:
    return " ".join(str(statement).split())


def test_repository_queries_are_tenant_product_and_version_scoped() -> None:
    product_sql = normalized(strategy_product_statement(TENANT_ID, "SYNTHETIC-001"))
    latest_sql = normalized(latest_strategy_statement(TENANT_ID, "SYNTHETIC-001"))
    version_sql = normalized(strategy_by_version_statement(TENANT_ID, "SYNTHETIC-001", 2))
    for sql in (product_sql, latest_sql, version_sql):
        assert "products.tenant_id" in sql
        assert "products.product_code" in sql
    assert "sales_strategy_inputs.version" in version_sql
    assert "FOR UPDATE" in product_sql


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class Session:
    def __init__(self, responses):
        self.responses = [Result(value) for value in responses]
        self.added = []
        self.flushed = False

    async def execute(self, statement):
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True


def candidate() -> SalesStrategyInputV1:
    value = SalesStrategyInputV1(
        tenant_id=TENANT_ID,
        product_code="SYNTHETIC-001",
        strategy_input_version=1,
        product_data_version=1,
        source_album_event_id="11111111-1111-4111-8111-111111111111",
        source_material_package_version=1,
        source_image_set_sha256="a" * 64,
        images=[
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "sha256": "b" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            }
        ],
        material={"value": "绿松石"},
        form={"value": "手串"},
        visual_observations=["蓝绿色圆珠"],
        selling_point_candidates=["颜色协调"],
        unverified_items=["真伪未验证"],
        forbidden_expressions=["保证升值"],
        created_at=datetime(2026, 8, 31, 8, tzinfo=UTC),
        content_sha256="c" * 64,
    )
    return value.model_copy(update={"content_sha256": canonical_sales_strategy_sha256(value)})


@pytest.mark.asyncio
async def test_append_allocates_next_version_and_canonical_digest() -> None:
    product = SimpleNamespace(id=UUID("55555555-5555-4555-8555-555555555555"))
    session = Session([product, None, 2])
    repository = SalesStrategyInputRepository(session)

    result = await repository.append_preview(candidate())

    assert result.created is True
    assert result.value.strategy_input_version == 3
    assert len(result.value.content_sha256) == 64
    assert session.added[0].version == 3
    assert session.added[0].payload == result.value.model_dump(mode="json")
    assert session.flushed is True


@pytest.mark.asyncio
async def test_append_returns_existing_identical_content_without_write() -> None:
    value = candidate()
    existing = SimpleNamespace(payload=value.model_dump(mode="json"))
    product = SimpleNamespace(id=UUID("55555555-5555-4555-8555-555555555555"))
    session = Session([product, existing])

    result = await SalesStrategyInputRepository(session).append_preview(value)

    assert result.created is False
    assert result.value == value
    assert session.added == []
