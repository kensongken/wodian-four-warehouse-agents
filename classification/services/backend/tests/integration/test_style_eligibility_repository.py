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
from product_engine.models.style_handoffs import (
    StyleEligibilityInputV2Record,
    StyleTransformAuthorizationRecord,
    StyleWarehouseHandoffRecord,
)
from product_engine.repositories.style_handoffs import (
    StyleHandoffRepository,
    StyleHandoffRepositoryFailure,
)
from product_engine.services.style_eligibility_v2 import StyleEligibilityPreparer

TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PRODUCT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PACKAGE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SOURCE_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
ASSET_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
CLASSIFICATION_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
AUTHORIZATION_REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")
NEW_AUTHORIZATION_REQUEST_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 9, 20, 8, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 9, 27, 8, tzinfo=UTC)


async def seed_classification(database: Database) -> None:
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
                reviewed_at=NOW,
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


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_concurrent_prepare_and_authorization_lifecycle() -> None:
    database_name = f"style_eligibility_test_{uuid4().hex}"
    admin = await asyncpg.connect(
        "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"
    )
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    database_url = (
        f"postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    )
    backend_root = Path(__file__).resolve().parents[2]
    database = Database(database_url)
    migration = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        assert migration.returncode == 0, migration.stderr
        await seed_classification(database)

        async def prepare():
            async with database.session() as session:
                return await StyleEligibilityPreparer(
                    StyleHandoffRepository(session, now=lambda: NOW), now=lambda: NOW
                ).prepare(TENANT_ID, CLASSIFICATION_ID)

        first, second = await asyncio.gather(prepare(), prepare())
        assert first.handoff.handoff_id == second.handoff.handoff_id
        assert {first.created, second.created} == {True, False}
        assert first.payload.transform_authorization_status == "none"

        async with database.session() as session:
            repository = StyleHandoffRepository(session, now=lambda: NOW)
            authorization = await repository.append_authorization(
                TENANT_ID,
                CLASSIFICATION_ID,
                authorization_request_id=AUTHORIZATION_REQUEST_ID,
                expires_at=EXPIRES_AT,
            )
            replay = await repository.append_authorization(
                TENANT_ID,
                CLASSIFICATION_ID,
                authorization_request_id=AUTHORIZATION_REQUEST_ID,
                expires_at=EXPIRES_AT,
            )
            assert replay.authorization_id == authorization.authorization_id
            with pytest.raises(StyleHandoffRepositoryFailure, match="style_authorization_conflict"):
                await repository.append_authorization(
                    TENANT_ID,
                    CLASSIFICATION_ID,
                    authorization_request_id=AUTHORIZATION_REQUEST_ID,
                    expires_at=datetime(2026, 9, 28, tzinfo=UTC),
                )

        authorized = await prepare()
        assert authorized.payload.transform_authorization_id == authorization.authorization_id
        assert authorized.payload.transform_authorization_status == "active"
        assert authorized.handoff.handoff_id != first.handoff.handoff_id
        assert authorized.eligibility_input_id != first.eligibility_input_id

        async with database.session() as session:
            repository = StyleHandoffRepository(session, now=lambda: NOW)
            revoked = await repository.revoke_authorization(
                TENANT_ID, authorization.authorization_id
            )
            revoked_replay = await repository.revoke_authorization(
                TENANT_ID, authorization.authorization_id
            )
            assert revoked_replay.revoked_at == revoked.revoked_at
            replacement = await repository.append_authorization(
                TENANT_ID,
                CLASSIFICATION_ID,
                authorization_request_id=NEW_AUTHORIZATION_REQUEST_ID,
                expires_at=datetime(2026, 9, 29, tzinfo=UTC),
            )
            assert replacement.authorization_id != authorization.authorization_id

        replaced = await prepare()
        assert replaced.payload.transform_authorization_id == replacement.authorization_id
        assert replaced.payload.transform_authorization_status == "active"

        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(StyleEligibilityInputV2Record)
                )
                == 3
            )
            assert (
                await session.scalar(select(func.count()).select_from(StyleWarehouseHandoffRecord))
                == 3
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(StyleTransformAuthorizationRecord)
                )
                == 2
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
