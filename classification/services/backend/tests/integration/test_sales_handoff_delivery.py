import asyncio
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
from sqlalchemy import func, select, update

from product_engine.contracts.sales_strategy_v2 import (
    SalesStrategyInputV2,
    canonical_sales_strategy_v2_sha256,
)
from product_engine.db import Database
from product_engine.models.classifications import ProductClassificationVersion
from product_engine.models.products import MaterialPackage, Product, SourceImage
from product_engine.models.sales_handoffs import (
    SalesStrategyInputV2Record,
    SalesWarehouseReceiptRecord,
    WarehouseHandoffRecord,
)
from product_engine.repositories.sales_handoffs import (
    SalesHandoffRepository,
    SalesWarehouseReceiptRepository,
)
from product_engine.security.signatures import sign_request
from product_engine.services.runtime_sales_handoffs import (
    RuntimeSalesHandoffFailure,
    RuntimeSalesHandoffService,
)
from product_engine.services.sales_warehouse_receiver import (
    DELIVERY_PATH,
    SalesWarehouseReceiver,
    SignedSalesDelivery,
)

NOW = 1_789_891_200
SECRET = b"sales-v2-integration-secret"
TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PRODUCT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLASSIFICATION_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
STRATEGY_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
HANDOFF_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
PACKAGE_ID = UUID("11111111-1111-4111-8111-111111111111")
SOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")


def payload() -> SalesStrategyInputV2:
    value = SalesStrategyInputV2(
        tenant_id=TENANT_ID,
        product_code="BCNHDIY914",
        strategy_input_version=1,
        source_material_package_version=1,
        source_classification_id=CLASSIFICATION_ID,
        source_classification_version=1,
        source_classification_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        images=(
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        material={"value": "信息不足", "evidence": "information_insufficient"},
        form={"value": "earring", "evidence": "human_confirmed"},
        unverified_items=("材质待人工确认",),
        created_at=datetime(2026, 9, 20, 8, tzinfo=UTC),
        content_sha256="0" * 64,
    )
    return value.model_copy(update={"content_sha256": canonical_sales_strategy_v2_sha256(value)})


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_receiver_replay_recovers_an_interrupted_sender_settlement() -> None:
    database_name = f"sales_delivery_test_{uuid4().hex}"
    admin = await asyncpg.connect(
        "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"
    )
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    database_url = (
        f"postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/{database_name}"
    )
    backend_root = Path(__file__).resolve().parents[2]
    database = Database(database_url)
    value = payload()
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
                    id=PRODUCT_ID,
                    tenant_id=TENANT_ID,
                    product_code=value.product_code,
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
                    sha256="c" * 64,
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
                    visual_observations=[],
                    selling_point_candidates=[],
                    inventory_status=None,
                    price_status="pending",
                    price_candidate=None,
                    service_rule_summaries=[],
                    source_image_set_sha256=value.source_image_set_sha256,
                    review_status="human_confirmed",
                    material_evidence="information_insufficient",
                    review_source="local_trainer_review",
                    reviewer_ref="local-operator",
                    reviewed_at=value.created_at,
                    request_id=uuid4(),
                    content_sha256=value.source_classification_sha256,
                )
            )
            await session.flush()
            session.add(
                SalesStrategyInputV2Record(
                    id=STRATEGY_ID,
                    tenant_id=TENANT_ID,
                    product_id=PRODUCT_ID,
                    version=1,
                    source_material_package_version=1,
                    source_classification_id=CLASSIFICATION_ID,
                    source_classification_version=1,
                    source_classification_sha256=value.source_classification_sha256,
                    source_image_set_sha256=value.source_image_set_sha256,
                    content_sha256=value.content_sha256,
                    status="preview_only",
                    payload=value.model_dump(mode="json"),
                    created_at=value.created_at,
                )
            )
            await session.flush()
            session.add(
                WarehouseHandoffRecord(
                    id=HANDOFF_ID,
                    tenant_id=TENANT_ID,
                    product_id=PRODUCT_ID,
                    strategy_input_id=STRATEGY_ID,
                    target_warehouse="sales",
                    payload_version=1,
                    payload_sha256=value.content_sha256,
                    idempotency_key=f"sales:{TENANT_ID}:{CLASSIFICATION_ID}",
                    status="prepared",
                    prepared_at=value.created_at,
                )
            )

        async with database.session() as session:
            delivery = await SalesHandoffRepository(session).mark_pending_and_build_delivery(
                TENANT_ID, HANDOFF_ID
            )
        body = json.dumps(
            delivery.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signed = SignedSalesDelivery(
            body=body,
            tenant_id=TENANT_ID,
            timestamp=NOW,
            signature=sign_request(
                SECRET,
                NOW,
                "classification",
                TENANT_ID,
                "POST",
                DELIVERY_PATH,
                body,
            ),
        )

        async def receive():
            async with database.session() as session:
                return await SalesWarehouseReceiver(
                    SalesWarehouseReceiptRepository(session),
                    SECRET,
                    now=lambda: NOW,
                ).receive(signed)

        first_receipt = await receive()
        async with database.session() as session:
            pending = await SalesHandoffRepository(session).read_handoff(TENANT_ID, HANDOFF_ID)
            assert pending is not None
            assert pending.handoff.status == "pending"
        accepted = await RuntimeSalesHandoffService(
            database=database,
            tenant_id=TENANT_ID,
            secret=SECRET,
            now=lambda: NOW,
        ).deliver(HANDOFF_ID)
        assert accepted.status == "accepted"
        assert accepted.receiver_receipt_id == first_receipt.receiver_receipt_id

        runtime = RuntimeSalesHandoffService(
            database=database,
            tenant_id=TENANT_ID,
            secret=SECRET,
            now=lambda: NOW,
        )
        replayed = await runtime.deliver(HANDOFF_ID)
        assert replayed.receiver_receipt_id == first_receipt.receiver_receipt_id

        async with database.session() as session:
            await session.execute(
                update(SalesWarehouseReceiptRecord)
                .where(SalesWarehouseReceiptRecord.id == first_receipt.receiver_receipt_id)
                .values(accepted_payload_sha256="f" * 64)
            )
        with pytest.raises(RuntimeSalesHandoffFailure, match="warehouse_handoff_conflict"):
            await runtime.deliver(HANDOFF_ID)

        async with database.session() as session:
            receipt_count = await session.scalar(
                select(func.count()).select_from(SalesWarehouseReceiptRecord)
            )
        assert receipt_count == 1
    finally:
        await database.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
