from importlib import import_module
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        if self.value is None:
            raise AssertionError("测试结果缺少必须值")
        return self.value


class FakeSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.statements = []
        self.added = []

    async def execute(self, statement):
        self.statements.append(statement)
        return ScalarResult(self.responses.pop(0))

    def add(self, value) -> None:
        self.added.append(value)


def event_store_modules():
    errors = import_module("product_engine.domain.errors")
    events = import_module("product_engine.models.events")
    repository = import_module("product_engine.repositories.events")
    return errors, events, repository


def test_inbox_claim_sql_is_atomic_and_conflict_safe() -> None:
    _, _, repository = event_store_modules()
    statement = repository.inbox_claim_statement(
        row_id=UUID("10000000-0000-4000-8000-000000000001"),
        tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
        source_system="album",
        event_id=UUID("11111111-1111-4111-8111-111111111111"),
        payload_sha256="a" * 64,
    )

    sql = str(statement.compile(dialect=postgresql.dialect())).replace("\n", " ")

    assert "ON CONFLICT (tenant_id, source_system, event_id) DO NOTHING" in sql
    assert "RETURNING inbox_events.id" in sql


def test_inbox_claim_is_scoped_by_tenant() -> None:
    _, _, repository = event_store_modules()
    statement = repository.inbox_claim_statement(
        row_id=UUID("10000000-0000-4000-8000-000000000001"),
        tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
        source_system="album",
        event_id=UUID("11111111-1111-4111-8111-111111111111"),
        payload_sha256="a" * 64,
    )

    sql = str(statement.compile(dialect=postgresql.dialect())).replace("\n", " ")

    assert "ON CONFLICT (tenant_id, source_system, event_id) DO NOTHING" in sql


async def test_same_event_and_payload_is_duplicate() -> None:
    _, _, repository = event_store_modules()
    event_id = UUID("11111111-1111-4111-8111-111111111111")
    tenant_id = UUID("22222222-2222-4222-8222-222222222222")
    inserted_id = UUID("10000000-0000-4000-8000-000000000001")
    session = FakeSession([inserted_id, None, "a" * 64])
    repo = repository.EventRepository(session)

    first = await repo.claim_inbox(tenant_id, "album", event_id, "a" * 64)
    second = await repo.claim_inbox(tenant_id, "album", event_id, "a" * 64)

    assert first.disposition == "new"
    assert second.disposition == "duplicate"


async def test_same_source_and_event_id_are_independent_between_tenants() -> None:
    _, _, repository = event_store_modules()
    event_id = UUID("11111111-1111-4111-8111-111111111111")
    first_insert = UUID("10000000-0000-4000-8000-000000000001")
    second_insert = UUID("10000000-0000-4000-8000-000000000002")
    session = FakeSession([first_insert, second_insert])
    repo = repository.EventRepository(session)

    first = await repo.claim_inbox(
        UUID("22222222-2222-4222-8222-222222222222"), "album", event_id, "a" * 64
    )
    second = await repo.claim_inbox(
        UUID("33333333-3333-4333-8333-333333333333"), "album", event_id, "a" * 64
    )

    assert first.disposition == "new"
    assert second.disposition == "new"


async def test_same_event_with_different_payload_conflicts() -> None:
    errors, _, repository = event_store_modules()
    event_id = UUID("11111111-1111-4111-8111-111111111111")
    tenant_id = UUID("22222222-2222-4222-8222-222222222222")
    session = FakeSession([None, "a" * 64])
    repo = repository.EventRepository(session)

    with pytest.raises(errors.EventConflict, match="event_id_reused_with_different_payload"):
        await repo.claim_inbox(tenant_id, "album", event_id, "b" * 64)


async def test_outbox_append_keeps_tenant_and_pending_status() -> None:
    _, _, repository = event_store_modules()
    tenant_id = UUID("22222222-2222-4222-8222-222222222222")
    session = FakeSession()
    repo = repository.EventRepository(session)

    outbox_id = await repo.append_outbox(
        tenant_id=tenant_id,
        topic="product-materials-updated.v1",
        aggregate_key="SP-00831",
        payload={"schema_version": 1},
    )

    assert isinstance(outbox_id, UUID)
    assert len(session.added) == 1
    event = session.added[0]
    assert event.id == outbox_id
    assert event.tenant_id == tenant_id
    assert event.status == "pending"
    assert event.payload == {"schema_version": 1}


def test_outbox_claim_query_is_available_and_skip_locked() -> None:
    _, _, repository = event_store_modules()

    statement = repository.pending_outbox_statement(10)
    sql = str(statement.compile(dialect=postgresql.dialect())).replace("\n", " ")

    assert "outbox_events.status" in sql
    assert "outbox_events.available_at <= now()" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
