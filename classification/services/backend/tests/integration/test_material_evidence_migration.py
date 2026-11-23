import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
PRODUCT_ID = UUID("22222222-2222-4222-8222-222222222222")
SOURCE_IMAGE_ID = UUID("33333333-3333-4333-8333-333333333333")
PACKAGE_ID = UUID("44444444-4444-4444-8444-444444444444")
CLASSIFICATION_ID = UUID("55555555-5555-4555-8555-555555555555")


async def _run_alembic(backend_root: Path, database_url: str, *args: str) -> None:
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", *args],
        cwd=backend_root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def _legacy_classification_snapshot(connection: asyncpg.Connection) -> str:
    value = await connection.fetchval(
        """
        SELECT (
            to_jsonb(classification_row)
            - 'material_claim_basis'
            - 'material_evidence_id'
        )::text
        FROM product_classification_versions AS classification_row
        WHERE id = $1
        """,
        CLASSIFICATION_ID,
    )
    assert isinstance(value, str)
    return value


async def _column_names(
    connection: asyncpg.Connection,
    table_name: str,
) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        """,
        table_name,
    )
    return {str(row["column_name"]) for row in rows}


async def _table_names(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        """
    )
    return {str(row["table_name"]) for row in rows}


async def _insert_legacy_classification(connection: asyncpg.Connection) -> None:
    await connection.execute(
        """
        INSERT INTO products (
            id, tenant_id, product_code, source_task_id, category, form,
            classification_confidence
        ) VALUES ($1, $2, 'BCNHDIY914', $3, 'unknown', 'earring', 0.5)
        """,
        PRODUCT_ID,
        TENANT_ID,
        uuid4(),
    )
    await connection.execute(
        """
        INSERT INTO source_images (
            id, tenant_id, product_id, upstream_asset_id, object_key, sha256,
            mime_type, kind, role, quality_confidence
        ) VALUES (
            $1, $2, $3, $4, 'local/main.jpg', $5, 'image/jpeg', 'original',
            'main_candidate', 1.0
        )
        """,
        SOURCE_IMAGE_ID,
        TENANT_ID,
        PRODUCT_ID,
        uuid4(),
        "a" * 64,
    )
    await connection.execute(
        """
        INSERT INTO material_packages (
            id, tenant_id, product_id, version, status, content_status,
            price_status, published_event_id
        ) VALUES (
            $1, $2, $3, 1, 'source_ready', 'pending', 'pending', $4
        )
        """,
        PACKAGE_ID,
        TENANT_ID,
        PRODUCT_ID,
        uuid4(),
    )
    await connection.execute(
        """
        INSERT INTO product_classification_versions (
            id, tenant_id, product_id, version, base_material_package_id,
            base_material_package_version, material_label, product_form,
            main_asset_id, visual_observations, selling_point_candidates,
            inventory_status, price_status, price_candidate,
            service_rule_summaries, source_image_set_sha256, review_status,
            material_evidence, review_source, reviewer_ref, reviewed_at,
            request_id, content_sha256
        ) VALUES (
            $1, $2, $3, 1, $4, 1, '信息不足', 'earring', $5,
            '[]'::jsonb, '[]'::jsonb, NULL, 'pending', NULL, '[]'::jsonb,
            $6, 'human_confirmed', 'information_insufficient',
            'local_trainer_review', 'local-operator', now(), $7, $8
        )
        """,
        CLASSIFICATION_ID,
        TENANT_ID,
        PRODUCT_ID,
        PACKAGE_ID,
        SOURCE_IMAGE_ID,
        "b" * 64,
        uuid4(),
        "c" * 64,
    )


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_0007_adds_evidence_tables_without_rewriting_existing_classifications() -> None:
    database_name = f"evidence_migration_{uuid4().hex}"
    admin = await asyncpg.connect(
        "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"
    )
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    async_url = (
        f"postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    )
    direct_url = f"postgresql://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    backend_root = Path(__file__).resolve().parents[2]
    connection: asyncpg.Connection | None = None

    try:
        await _run_alembic(
            backend_root,
            async_url,
            "upgrade",
            "0006_style_warehouse_v2_handoffs",
        )
        connection = await asyncpg.connect(direct_url)
        await _insert_legacy_classification(connection)
        before = await _legacy_classification_snapshot(connection)

        await _run_alembic(backend_root, async_url, "upgrade", "head")
        assert await _legacy_classification_snapshot(connection) == before
        assert await _column_names(
            connection,
            "product_classification_versions",
        ) >= {"material_claim_basis", "material_evidence_id"}
        assert await _table_names(connection) >= {
            "material_evidence_uploads",
            "material_evidence_records",
            "material_evidence_revocations",
        }

        await _run_alembic(
            backend_root,
            async_url,
            "downgrade",
            "0006_style_warehouse_v2_handoffs",
        )
        assert await _legacy_classification_snapshot(connection) == before
        assert "material_claim_basis" not in await _column_names(
            connection,
            "product_classification_versions",
        )

        await _run_alembic(backend_root, async_url, "upgrade", "head")
        assert await _legacy_classification_snapshot(connection) == before
    finally:
        if connection is not None:
            await connection.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
