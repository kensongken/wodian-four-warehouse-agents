import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import func, select

from product_engine.contracts.album import ProductImageSetReadyV1, SourceImageRef
from product_engine.db import Database
from product_engine.models.events import InboxEvent, OutboxEvent
from product_engine.models.products import MaterialPackage
from product_engine.services.employee_batch_promotions import EmployeeBatchPromotionFailure
from product_engine.services.runtime_employee_batch_promotions import (
    RuntimeEmployeePromotionWriter,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("22222222-2222-4222-8222-222222222222")
BATCH_ID = "BATCH-20260921035620-3A284AA4"


def six_events(*, duplicate_sixth_asset: bool = False) -> tuple[ProductImageSetReadyV1, ...]:
    events: list[ProductImageSetReadyV1] = []
    codes = ("914", "933", "951", "952", "997", "1047")
    for product_index, suffix in enumerate(codes, start=1):
        asset_id = UUID(int=1 if duplicate_sixth_asset and product_index == 6 else product_index)
        events.append(
            ProductImageSetReadyV1(
                schema_version=1,
                event_id=UUID(int=100 + product_index),
                occurred_at=datetime(2026, 9, 21, tzinfo=UTC),
                tenant_id=TENANT_ID,
                source_task_id=UUID(int=500),
                product_code=f"BCNHDIY{suffix}",
                category="信息不足",
                form="unknown",
                classification_confidence=0.0,
                images=[
                    SourceImageRef(
                        asset_id=asset_id,
                        object_key=f"tenants/{TENANT_ID}/employee-handoffs/b/{suffix}/{asset_id}",
                        sha256=f"{product_index:x}" * 64,
                        mime_type="image/jpeg",
                        kind="original",
                        role="main_candidate",
                        quality_confidence=0.0,
                    )
                ],
            )
        )
    return tuple(events)


class NeverDatabase:
    @asynccontextmanager
    async def session(self) -> AsyncIterator[object]:
        raise AssertionError("database session must not open")
        yield object()


async def test_runtime_writer_rejects_mixed_tenant_before_opening_transaction() -> None:
    events = list(six_events())
    events[0] = events[0].model_copy(update={"tenant_id": UUID(int=999)})
    writer = RuntimeEmployeePromotionWriter(NeverDatabase(), TENANT_ID)

    with pytest.raises(EmployeeBatchPromotionFailure, match="employee_handoff_ledger_conflict"):
        await writer.commit_batch(
            request_id=REQUEST_ID,
            batch_id=BATCH_ID,
            content_sha256="a" * 64,
            events=tuple(events),
        )


@asynccontextmanager
async def isolated_database() -> AsyncIterator[Database]:
    database_name = f"employee_promotion_test_{uuid4().hex}"
    admin = await asyncpg.connect(
        "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"
    )
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    database_url = (
        f"postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    )
    database = Database(database_url)
    backend_root = Path(__file__).resolve().parents[2]
    try:
        migration = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=backend_root,
            env={**os.environ, "DATABASE_URL": database_url},
            capture_output=True,
            text=True,
            check=False,
        )
        assert migration.returncode == 0, migration.stderr
        yield database
    finally:
        await database.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_exact_replay_returns_original_versions_without_outbox() -> None:
    async with isolated_database() as database:
        writer = RuntimeEmployeePromotionWriter(database, TENANT_ID)

        first = await writer.commit_batch(
            request_id=REQUEST_ID,
            batch_id=BATCH_ID,
            content_sha256="a" * 64,
            events=six_events(),
        )
        replay = await writer.commit_batch(
            request_id=REQUEST_ID,
            batch_id=BATCH_ID,
            content_sha256="a" * 64,
            events=six_events(),
        )

        assert replay == first
        async with database.session() as session:
            assert (await session.scalar(select(func.count(MaterialPackage.id)))) == 6
            assert (await session.scalar(select(func.count(OutboxEvent.id)))) == 0
            assert (
                await session.scalar(
                    select(func.count(InboxEvent.id)).where(
                        InboxEvent.source_system == "employee_import_promotion"
                    )
                )
            ) == 6


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_sixth_product_failure_rolls_back_first_five() -> None:
    async with isolated_database() as database:
        writer = RuntimeEmployeePromotionWriter(database, TENANT_ID)

        with pytest.raises(EmployeeBatchPromotionFailure):
            await writer.commit_batch(
                request_id=REQUEST_ID,
                batch_id=BATCH_ID,
                content_sha256="a" * 64,
                events=six_events(duplicate_sixth_asset=True),
            )

        async with database.session() as session:
            assert (await session.scalar(select(func.count(MaterialPackage.id)))) == 0
            assert (await session.scalar(select(func.count(InboxEvent.id)))) == 0
