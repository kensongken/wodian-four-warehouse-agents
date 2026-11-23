from types import SimpleNamespace
from uuid import UUID

import pytest

from product_engine.contracts.sales import SalesMaterialUsageV1
from product_engine.domain.errors import MaterialUsageReferenceNotFound
from product_engine.repositories.usage import UsageRepository

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
MATERIAL_ID = UUID("44444444-4444-4444-8444-444444444444")


class ScalarRows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self.values)


class Row:
    def __init__(self, value):
        self.value = value

    def one_or_none(self):
        return self.value


class Session:
    def __init__(self, package, material_ids):
        self.responses = [Row(package), ScalarRows(material_ids)]
        self.added = []
        self.flushed = False

    async def execute(self, statement):
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True


def event(*, material_ids=None) -> SalesMaterialUsageV1:
    return SalesMaterialUsageV1(
        schema_version=1,
        event_id="11111111-1111-4111-8111-111111111111",
        occurred_at="2026-08-15T08:00:00Z",
        tenant_id=TENANT_ID,
        session_id="33333333-3333-4333-8333-333333333333",
        product_code="SP-00831",
        package_version=1,
        usage_type="copied",
        material_ids=material_ids or [MATERIAL_ID],
    )


@pytest.mark.asyncio
async def test_usage_repository_persists_only_package_membership() -> None:
    session = Session(
        (
            SimpleNamespace(id=UUID("55555555-5555-4555-8555-555555555555")),
            SimpleNamespace(id=UUID("66666666-6666-4666-8666-666666666666")),
        ),
        [MATERIAL_ID],
    )

    result = await UsageRepository(session).record(event(), "a" * 64)

    assert result.disposition == "accepted"
    assert session.flushed is True
    assert session.added[0].material_ids == [str(MATERIAL_ID)]


@pytest.mark.asyncio
async def test_usage_repository_rejects_material_not_in_package() -> None:
    session = Session(
        (
            SimpleNamespace(id=UUID("55555555-5555-4555-8555-555555555555")),
            SimpleNamespace(id=UUID("66666666-6666-4666-8666-666666666666")),
        ),
        [],
    )

    with pytest.raises(MaterialUsageReferenceNotFound, match="material_not_in_package"):
        await UsageRepository(session).record(event(), "a" * 64)
    assert session.added == []
