import asyncio
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import UniqueConstraint, func, select
from sqlalchemy.dialects import postgresql

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
)
from product_engine.db import Database
from product_engine.models.classifications import ProductClassificationVersion
from product_engine.models.products import (
    MaterialPackage,
    MaterialPackageAsset,
    Product,
    SourceImage,
)
from product_engine.repositories.classifications import (
    ClassificationLedgerRepository,
    ClassificationSourceAsset,
    ClassificationWriteContext,
    locked_product_statement,
)


def test_version_allocation_is_guarded_by_product_row_lock_and_unique_key() -> None:
    statement = locked_product_statement(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    )
    sql = " ".join(str(statement.compile(dialect=postgresql.dialect())).split())
    unique_names = {
        constraint.name
        for constraint in ProductClassificationVersion.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert "FOR UPDATE" in sql
    assert "products.tenant_id" in sql
    assert "products.id" in sql
    assert "uq_classification_versions_tenant_product_version" in unique_names


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_two_concurrent_appends_create_only_version_one() -> None:
    database_name = f"classification_repository_test_{uuid4().hex}"
    admin = await asyncpg.connect(
        "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"
    )
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    database_url = (
        f"postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    )
    backend_root = Path(__file__).resolve().parents[2]
    database = Database(database_url)
    tenant_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    product_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    package_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    main_source_id = UUID("44444444-4444-4444-8444-444444444444")
    gallery_source_id = UUID("55555555-5555-4555-8555-555555555555")
    main_asset_id = UUID("22222222-2222-4222-8222-222222222222")
    gallery_asset_id = UUID("33333333-3333-4333-8333-333333333333")

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

        async with database.session() as session:
            session.add(
                Product(
                    id=product_id,
                    tenant_id=tenant_id,
                    product_code="BCNHDIY914",
                    source_task_id=uuid4(),
                    category="unknown",
                    form="earring",
                    classification_confidence=Decimal("0.5000"),
                )
            )
            await session.flush()
            session.add_all(
                [
                    SourceImage(
                        id=main_source_id,
                        tenant_id=tenant_id,
                        product_id=product_id,
                        upstream_asset_id=main_asset_id,
                        object_key="local/main.jpg",
                        sha256="a" * 64,
                        mime_type="image/jpeg",
                        kind="original",
                        role="main_candidate",
                        quality_confidence=Decimal("1.0000"),
                    ),
                    SourceImage(
                        id=gallery_source_id,
                        tenant_id=tenant_id,
                        product_id=product_id,
                        upstream_asset_id=gallery_asset_id,
                        object_key="local/gallery.jpg",
                        sha256="b" * 64,
                        mime_type="image/jpeg",
                        kind="original",
                        role="gallery",
                        quality_confidence=Decimal("1.0000"),
                    ),
                ]
            )
            session.add(
                MaterialPackage(
                    id=package_id,
                    tenant_id=tenant_id,
                    product_id=product_id,
                    version=1,
                    status="source_ready",
                    content_status="pending",
                    price_status="pending",
                    published_event_id=uuid4(),
                )
            )
            await session.flush()
            session.add_all(
                [
                    MaterialPackageAsset(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        package_id=package_id,
                        source_image_id=main_source_id,
                        position=0,
                    ),
                    MaterialPackageAsset(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        package_id=package_id,
                        source_image_id=gallery_source_id,
                        position=1,
                    ),
                ]
            )

        context = ClassificationWriteContext(
            product_id=product_id,
            latest_material_package_version=1,
            requested_material_package_id=package_id,
            assets=(
                ClassificationSourceAsset(
                    upstream_asset_id=main_asset_id,
                    source_image_id=main_source_id,
                    position=0,
                    role="main_candidate",
                    sha256="a" * 64,
                ),
                ClassificationSourceAsset(
                    upstream_asset_id=gallery_asset_id,
                    source_image_id=gallery_source_id,
                    position=1,
                    role="gallery",
                    sha256="b" * 64,
                ),
            ),
        )

        def command(request_id: UUID) -> ClassificationConfirmationCommand:
            return ClassificationConfirmationCommand(
                request_id=request_id,
                product_code="BCNHDIY914",
                local_batch_id="BATCH-20260920033509-8C21C203",
                base_material_package_version=1,
                material_label="信息不足",
                product_form="earring",
                main_asset_id=main_asset_id,
                visual_observations=[],
                selling_point_candidates=[],
                inventory_status=None,
                price_status="pending",
                price_candidate=None,
                service_rule_summaries=[],
                review_source="local_trainer_review",
                assets=[
                    ClassificationAssetConfirmation(
                        asset_id=main_asset_id,
                        position=0,
                        role="main_candidate",
                        sha256="a" * 64,
                    ),
                    ClassificationAssetConfirmation(
                        asset_id=gallery_asset_id,
                        position=1,
                        role="gallery",
                        sha256="b" * 64,
                    ),
                ],
            )

        async def append(request_id: UUID):
            async with database.session() as session:
                return await ClassificationLedgerRepository(session, tenant_id).append(
                    command(request_id), context, "local-operator"
                )

        first, second = await asyncio.gather(append(uuid4()), append(uuid4()))
        assert {first.disposition, second.disposition} == {"created", "duplicate"}

        async with database.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ProductClassificationVersion)
                .where(
                    ProductClassificationVersion.tenant_id == tenant_id,
                    ProductClassificationVersion.product_id == product_id,
                )
            )
            versions = list(
                (
                    await session.scalars(
                        select(ProductClassificationVersion.version).where(
                            ProductClassificationVersion.tenant_id == tenant_id,
                            ProductClassificationVersion.product_id == product_id,
                        )
                    )
                ).all()
            )
        assert count == 1
        assert versions == [1]
    finally:
        await database.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
