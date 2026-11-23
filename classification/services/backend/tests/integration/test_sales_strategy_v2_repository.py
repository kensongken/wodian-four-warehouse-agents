import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import func, select

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    canonical_classification_sha256,
)
from product_engine.db import Database
from product_engine.models.classifications import (
    ProductClassificationAsset,
    ProductClassificationVersion,
)
from product_engine.models.products import (
    MaterialPackage,
    MaterialPackageAsset,
    Product,
    SourceImage,
)
from product_engine.models.sales_handoffs import (
    SalesStrategyInputV2Record,
    WarehouseHandoffRecord,
)
from product_engine.repositories.sales_handoffs import SalesHandoffRepository
from product_engine.services.sales_strategy_v2 import SalesPreviewPreparer

TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PRODUCT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PACKAGE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SOURCE_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
ASSET_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
CLASSIFICATION_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_concurrent_prepare_creates_one_v2_and_one_handoff() -> None:
    database_name = f"sales_v2_repository_test_{uuid4().hex}"
    admin = await asyncpg.connect(
        "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"
    )
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    database_url = (
        f"postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    )
    backend_root = Path(__file__).resolve().parents[2]
    database = Database(database_url)
    command = ClassificationConfirmationCommand(
        request_id=uuid4(),
        product_code="BCNHDIY914",
        local_batch_id="BATCH-INTEGRATION",
        base_material_package_version=1,
        material_label="信息不足",
        product_form="earring",
        main_asset_id=ASSET_ID,
        visual_observations=["金色圆环外形"],
        selling_point_candidates=["日常佩戴"],
        inventory_status=None,
        price_status="pending",
        price_candidate=None,
        service_rule_summaries=[],
        review_source="local_trainer_review",
        assets=[
            ClassificationAssetConfirmation(
                asset_id=ASSET_ID,
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            )
        ],
    )

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
        source_image_set_sha256 = hashlib.sha256(
            json.dumps(
                [command.assets[0].model_dump(mode="json")],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with database.session() as session:
            session.add(
                Product(
                    id=PRODUCT_ID,
                    tenant_id=TENANT_ID,
                    product_code="BCNHDIY914",
                    source_task_id=uuid4(),
                    category="unknown",
                    form="earring",
                    classification_confidence=Decimal("0.5000"),
                )
            )
            await session.flush()
            session.add(
                SourceImage(
                    id=SOURCE_ID,
                    tenant_id=TENANT_ID,
                    product_id=PRODUCT_ID,
                    upstream_asset_id=ASSET_ID,
                    object_key="local/main.jpg",
                    sha256="a" * 64,
                    mime_type="image/jpeg",
                    kind="original",
                    role="main_candidate",
                    quality_confidence=Decimal("1.0000"),
                )
            )
            session.add(
                MaterialPackage(
                    id=PACKAGE_ID,
                    tenant_id=TENANT_ID,
                    product_id=PRODUCT_ID,
                    version=1,
                    status="source_ready",
                    content_status="pending",
                    price_status="pending",
                    published_event_id=uuid4(),
                )
            )
            await session.flush()
            session.add(
                MaterialPackageAsset(
                    id=uuid4(),
                    tenant_id=TENANT_ID,
                    package_id=PACKAGE_ID,
                    source_image_id=SOURCE_ID,
                    position=0,
                )
            )
            session.add(
                ProductClassificationVersion(
                    id=CLASSIFICATION_ID,
                    tenant_id=TENANT_ID,
                    product_id=PRODUCT_ID,
                    version=1,
                    base_material_package_id=PACKAGE_ID,
                    base_material_package_version=1,
                    material_label="信息不足",
                    product_form="earring",
                    main_asset_id=SOURCE_ID,
                    visual_observations=command.visual_observations,
                    selling_point_candidates=command.selling_point_candidates,
                    inventory_status=None,
                    price_status="pending",
                    price_candidate=None,
                    service_rule_summaries=[],
                    source_image_set_sha256=source_image_set_sha256,
                    review_status="human_confirmed",
                    material_evidence="information_insufficient",
                    review_source="local_trainer_review",
                    reviewer_ref="local-operator",
                    reviewed_at=datetime.now(UTC),
                    request_id=command.request_id,
                    content_sha256=canonical_classification_sha256(command),
                )
            )
            await session.flush()
            session.add(
                ProductClassificationAsset(
                    id=uuid4(),
                    tenant_id=TENANT_ID,
                    classification_version_id=CLASSIFICATION_ID,
                    source_image_id=SOURCE_ID,
                    position=0,
                    role="main_candidate",
                    sha256="a" * 64,
                )
            )

        async def prepare():
            async with database.session() as session:
                return await SalesPreviewPreparer(SalesHandoffRepository(session)).prepare(
                    TENANT_ID, CLASSIFICATION_ID
                )

        first, second = await asyncio.gather(prepare(), prepare())

        assert first.handoff.handoff_id == second.handoff.handoff_id
        assert {first.created, second.created} == {True, False}
        async with database.session() as session:
            inputs = await session.scalar(
                select(func.count()).select_from(SalesStrategyInputV2Record)
            )
            handoffs = await session.scalar(
                select(func.count()).select_from(WarehouseHandoffRecord)
            )
        assert inputs == 1
        assert handoffs == 1
    finally:
        await database.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
