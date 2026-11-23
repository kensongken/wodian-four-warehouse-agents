import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import func, select

from product_engine.db import Database
from product_engine.models.style_handoffs import (
    StyleWarehouseDecisionReceiptRecord,
)
from product_engine.repositories.style_handoffs import StyleHandoffRepository
from product_engine.services.runtime_style_handoffs import RuntimeStyleHandoffService
from product_engine.services.style_warehouse_receiver import StyleWarehouseReceiver

from .test_style_warehouse_decisions import (
    HANDOFF_ID,
    NOW,
    NOW_SECONDS,
    SECRET,
    TENANT_ID,
    payload,
    seed,
    signed,
)


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_runtime_recovers_receiver_decision_before_sender_settlement() -> None:
    database_name = f"style_runtime_test_{uuid4().hex}"
    admin = await asyncpg.connect(
        "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"
    )
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    database_url = (
        f"postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    )
    backend_root = Path(__file__).resolve().parents[2]
    database = Database(database_url)
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
        await seed(database, payload())
        async with database.session() as session:
            repository = StyleHandoffRepository(session, now=lambda: NOW)
            delivery = await repository.mark_pending_and_build_delivery(TENANT_ID, HANDOFF_ID)
        body = json.dumps(
            delivery.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        async with database.session() as session:
            first_receipt = await StyleWarehouseReceiver(
                StyleHandoffRepository(session, now=lambda: NOW),
                SECRET,
                now_seconds=lambda: NOW_SECONDS,
                now_datetime=lambda: NOW,
            ).receive(signed(body))

        before_recovery = await RuntimeStyleHandoffService(
            database=database,
            tenant_id=TENANT_ID,
            secret=SECRET,
            now=lambda: NOW_SECONDS,
        ).read(HANDOFF_ID)
        assert before_recovery.handoff.status == "pending"
        assert before_recovery.handoff.decision_receipt_id is None

        restored = await RuntimeStyleHandoffService(
            database=database,
            tenant_id=TENANT_ID,
            secret=SECRET,
            now=lambda: NOW_SECONDS,
        ).evaluate(HANDOFF_ID)
        assert restored.handoff.status == "rejected"
        assert restored.handoff.decision_receipt_id == first_receipt.decision_receipt_id
        assert restored.handoff.rejection_code == "classification_material_unconfirmed"

        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(StyleWarehouseDecisionReceiptRecord)
                )
                == 1
            )
    finally:
        await database.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
