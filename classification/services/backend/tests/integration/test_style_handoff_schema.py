import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from product_engine.models.style_handoffs import (
    StyleEligibilityInputV2Record,
    StyleTransformAuthorizationRecord,
    StyleWarehouseDecisionReceiptRecord,
    StyleWarehouseHandoffRecord,
)


def test_style_sender_and_receiver_use_separate_tables() -> None:
    assert StyleEligibilityInputV2Record.__tablename__ == "style_eligibility_inputs_v2"
    assert StyleTransformAuthorizationRecord.__tablename__ == ("style_transform_authorizations")
    assert StyleWarehouseHandoffRecord.__tablename__ == "style_warehouse_handoffs"
    assert StyleWarehouseDecisionReceiptRecord.__tablename__ == (
        "style_warehouse_decision_receipts"
    )
    assert StyleWarehouseHandoffRecord.__table__ is not (
        StyleWarehouseDecisionReceiptRecord.__table__
    )


def test_style_decision_never_requires_a_generation_job() -> None:
    column = StyleWarehouseDecisionReceiptRecord.__table__.c.generation_job_id
    assert column.nullable is True


def test_style_schema_contains_constraints_and_indexes(alembic_runner) -> None:
    result = alembic_runner("upgrade", "head", "--sql")

    assert result.returncode == 0, result.stderr
    sql = result.stdout.lower()
    for table in (
        "style_eligibility_inputs_v2",
        "style_transform_authorizations",
        "style_warehouse_handoffs",
        "style_warehouse_decision_receipts",
    ):
        assert f"create table {table}" in sql
    for name in (
        "uq_style_inputs_tenant_product_version",
        "uq_style_inputs_tenant_product_content",
        "uq_style_authorizations_tenant_request",
        "uq_style_handoffs_tenant_key",
        "uq_style_handoffs_tenant_input",
        "uq_style_decisions_tenant_handoff",
        "uq_style_decisions_tenant_key",
        "ck_style_handoffs_status",
        "ck_style_decisions_generation_empty",
        "ck_style_decisions_rejection_code",
        "ix_style_authorizations_tenant_source_scope",
    ):
        assert name in sql


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
@pytest.mark.parametrize("starting_revision", ["base", "0005_sales_strategy_v2_handoffs"])
async def test_style_migration_upgrades_empty_and_existing_databases(
    starting_revision: str,
) -> None:
    database_name = f"style_schema_test_{uuid4().hex}"
    admin = await asyncpg.connect(
        "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"
    )
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    database_url = (
        f"postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    )
    backend_root = Path(__file__).resolve().parents[2]

    async def migrate(revision: str) -> subprocess.CompletedProcess[str]:
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=backend_root,
            env={**os.environ, "DATABASE_URL": database_url},
            capture_output=True,
            text=True,
            check=False,
        )

    try:
        first = await migrate(starting_revision)
        assert first.returncode == 0, first.stderr
        head = await migrate("head")
        assert head.returncode == 0, head.stderr
        connection = await asyncpg.connect(database_url.replace("+asyncpg", ""))
        try:
            rows = await connection.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'style_%'"
            )
            assert {row["table_name"] for row in rows} == {
                "style_eligibility_inputs_v2",
                "style_transform_authorizations",
                "style_warehouse_handoffs",
                "style_warehouse_decision_receipts",
            }
        finally:
            await connection.close()
    finally:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
