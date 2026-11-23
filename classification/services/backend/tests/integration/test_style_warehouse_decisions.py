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
from sqlalchemy import func, select

from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityInputV2,
    canonical_style_eligibility_v2_sha256,
)
from product_engine.db import Database
from product_engine.models.classifications import ProductClassificationVersion
from product_engine.models.products import MaterialPackage, Product, SourceImage
from product_engine.models.style_handoffs import (
    StyleEligibilityInputV2Record,
    StyleWarehouseDecisionReceiptRecord,
    StyleWarehouseHandoffRecord,
)
from product_engine.repositories.style_handoffs import StyleHandoffRepository
from product_engine.security.signatures import sign_request
from product_engine.services.style_warehouse_receiver import (
    DELIVERY_PATH,
    SignedStyleDelivery,
    StyleReceiverFailure,
    StyleWarehouseReceiver,
)

NOW_SECONDS = 1_790_000_000
NOW = datetime.fromtimestamp(NOW_SECONDS, UTC)
SECRET = b"style-v2-integration-secret"
TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PRODUCT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PACKAGE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SOURCE_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
ASSET_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
CLASSIFICATION_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
ELIGIBILITY_ID = UUID("11111111-1111-4111-8111-111111111111")
HANDOFF_ID = UUID("22222222-2222-4222-8222-222222222222")


def payload() -> StyleEligibilityInputV2:
    value = StyleEligibilityInputV2(
        tenant_id=TENANT_ID,
        product_code="BCNHDIY914",
        eligibility_input_version=1,
        source_material_package_version=1,
        source_classification_id=CLASSIFICATION_ID,
        source_classification_version=1,
        source_classification_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        main_image={
            "asset_id": ASSET_ID,
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        },
        images=(
            {
                "asset_id": ASSET_ID,
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        material={"value": "信息不足", "evidence": "information_insufficient"},
        form={"value": "earring", "evidence": "human_confirmed"},
        transform_authorization_id=None,
        transform_authorization_status="none",
        created_at=NOW,
        content_sha256="0" * 64,
    )
    return value.model_copy(update={"content_sha256": canonical_style_eligibility_v2_sha256(value)})


async def seed(database: Database, value: StyleEligibilityInputV2) -> None:
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
                reviewed_at=NOW,
                request_id=uuid4(),
                content_sha256=value.source_classification_sha256,
            )
        )
        await session.flush()
        session.add(
            StyleEligibilityInputV2Record(
                id=ELIGIBILITY_ID,
                tenant_id=TENANT_ID,
                product_id=PRODUCT_ID,
                version=1,
                source_material_package_version=1,
                source_classification_id=CLASSIFICATION_ID,
                source_classification_version=1,
                source_classification_sha256=value.source_classification_sha256,
                source_image_set_sha256=value.source_image_set_sha256,
                transform_authorization_id=None,
                transform_authorization_status="none",
                content_sha256=value.content_sha256,
                status="eligibility_only",
                payload=value.model_dump(mode="json"),
                created_at=NOW,
            )
        )
        await session.flush()
        session.add(
            StyleWarehouseHandoffRecord(
                id=HANDOFF_ID,
                tenant_id=TENANT_ID,
                product_id=PRODUCT_ID,
                eligibility_input_id=ELIGIBILITY_ID,
                payload_version=1,
                payload_sha256=value.content_sha256,
                idempotency_key=f"style:{TENANT_ID}:{ELIGIBILITY_ID}",
                status="prepared",
                prepared_at=NOW,
            )
        )


def signed(body: bytes) -> SignedStyleDelivery:
    return SignedStyleDelivery(
        body=body,
        tenant_id=TENANT_ID,
        timestamp=NOW_SECONDS,
        signature=sign_request(
            SECRET,
            NOW_SECONDS,
            "classification",
            TENANT_ID,
            "POST",
            DELIVERY_PATH,
            body,
        ),
    )


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_rejection_replay_and_conflict_are_durable() -> None:
    database_name = f"style_decision_test_{uuid4().hex}"
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
        await seed(database, value)
        async with database.session() as session:
            delivery = await StyleHandoffRepository(
                session, now=lambda: NOW
            ).mark_pending_and_build_delivery(TENANT_ID, HANDOFF_ID)
        body = json.dumps(
            delivery.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

        async def receive(request: SignedStyleDelivery):
            async with database.session() as session:
                return await StyleWarehouseReceiver(
                    StyleHandoffRepository(session, now=lambda: NOW),
                    SECRET,
                    now_seconds=lambda: NOW_SECONDS,
                    now_datetime=lambda: NOW,
                ).receive(request)

        first, replay = await asyncio.gather(receive(signed(body)), receive(signed(body)))
        assert first.decision == "rejected"
        assert first.rejection_code == "classification_material_unconfirmed"
        assert first.generation_job_id is None
        assert replay.decision_receipt_id == first.decision_receipt_id

        raw = delivery.model_dump(mode="json")
        raw["payload"]["source_classification_sha256"] = "f" * 64
        changed_payload = StyleEligibilityInputV2.model_validate(raw["payload"])
        changed_digest = canonical_style_eligibility_v2_sha256(changed_payload)
        raw["payload"]["content_sha256"] = changed_digest
        raw["payload_sha256"] = changed_digest
        changed_body = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        with pytest.raises(StyleReceiverFailure, match="style_handoff_conflict"):
            await receive(signed(changed_body))

        async with database.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(StyleWarehouseDecisionReceiptRecord)
                )
                == 1
            )
            record = await session.get(StyleWarehouseHandoffRecord, HANDOFF_ID)
            assert record is not None
            assert record.status == "pending"
            assert record.decision_receipt_id is None
            assert record.rejection_code is None
    finally:
        await database.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
