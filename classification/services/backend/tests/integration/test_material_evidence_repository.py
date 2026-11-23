import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import func, select

from product_engine.contracts.material_evidence import (
    MaterialEvidenceInsufficientCommand,
    MaterialEvidenceReceipt,
    MaterialEvidenceRegistrationCommand,
    MaterialEvidenceRevocationCommand,
)
from product_engine.db import Database
from product_engine.models.classifications import (
    ProductClassificationAsset,
    ProductClassificationVersion,
)
from product_engine.models.material_evidence import (
    MaterialEvidenceRecord,
    MaterialEvidenceRevocationRecord,
)
from product_engine.models.products import MaterialPackage, Product, SourceImage
from product_engine.repositories.material_evidence import (
    MaterialEvidenceActorContext,
    MaterialEvidenceFailure,
    MaterialEvidenceRepository,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
PRODUCT_ID = UUID("22222222-2222-4222-8222-222222222222")
SOURCE_IMAGE_ID = UUID("33333333-3333-4333-8333-333333333333")
PACKAGE_ID = UUID("44444444-4444-4444-8444-444444444444")
BASE_CLASSIFICATION_ID = UUID("55555555-5555-4555-8555-555555555555")
ASSET_ID = UUID("66666666-6666-4666-8666-666666666666")
REQUEST_1 = UUID("77777777-7777-4777-8777-777777777777")
REQUEST_2 = UUID("88888888-8888-4888-8888-888888888888")
REQUEST_3 = UUID("99999999-9999-4999-8999-999999999999")
REQUEST_4 = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def registration(request_id: UUID, evidence_ref: str) -> MaterialEvidenceRegistrationCommand:
    return MaterialEvidenceRegistrationCommand(
        request_id=request_id,
        product_code="BCNHDIY914",
        base_classification_id=BASE_CLASSIFICATION_ID,
        material_label="南红",
        claim_basis="merchant_declared",
        evidence_kind="merchant_declaration",
        evidence_ref=evidence_ref,
        evidence_sha256=None,
        source_summary="商家确认当前商品材质为南红",
        acknowledge_not_authentication=True,
    )


def actor_context() -> MaterialEvidenceActorContext:
    return MaterialEvidenceActorContext(
        declared_by_ref="merchant-local-01",
        reviewer_ref="trainer-local-01",
    )


async def _migrate(backend_root: Path, database_url: str) -> None:
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def _seed_base_classification(database: Database) -> None:
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
                id=SOURCE_IMAGE_ID,
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
            ProductClassificationVersion(
                id=BASE_CLASSIFICATION_ID,
                tenant_id=TENANT_ID,
                product_id=PRODUCT_ID,
                version=1,
                base_material_package_id=PACKAGE_ID,
                base_material_package_version=1,
                material_label="信息不足",
                product_form="earring",
                main_asset_id=SOURCE_IMAGE_ID,
                visual_observations=["红色珠串"],
                selling_point_candidates=["日常佩戴"],
                inventory_status=None,
                price_status="pending",
                price_candidate=None,
                service_rule_summaries=[],
                source_image_set_sha256="b" * 64,
                review_status="human_confirmed",
                material_evidence="information_insufficient",
                material_claim_basis="information_insufficient",
                material_evidence_id=None,
                review_source="local_trainer_review",
                reviewer_ref="trainer-local-01",
                reviewed_at=datetime(2026, 9, 21, 2, tzinfo=UTC),
                request_id=uuid4(),
                content_sha256="c" * 64,
            )
        )
        await session.flush()
        session.add(
            ProductClassificationAsset(
                id=uuid4(),
                tenant_id=TENANT_ID,
                classification_version_id=BASE_CLASSIFICATION_ID,
                source_image_id=SOURCE_IMAGE_ID,
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            )
        )


@asynccontextmanager
async def _seeded_database() -> AsyncIterator[Database]:
    database_name = f"evidence_repository_{uuid4().hex}"
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
        await _migrate(backend_root, database_url)
        await _seed_base_classification(database)
        yield database
    finally:
        await database.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()


async def _register(
    database: Database,
    request_id: UUID = REQUEST_1,
    evidence_ref: str = "material-declaration:first",
) -> MaterialEvidenceReceipt:
    async with database.session() as session:
        return await MaterialEvidenceRepository(session, TENANT_ID).register(
            registration(request_id, evidence_ref),
            actor_context(),
        )


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_concurrent_registration_creates_one_latest_version() -> None:
    async with _seeded_database() as database:

        async def register(
            request_id: UUID,
            evidence_ref: str,
        ) -> MaterialEvidenceReceipt:
            async with database.session() as session:
                return await MaterialEvidenceRepository(session, TENANT_ID).register(
                    registration(request_id, evidence_ref),
                    actor_context(),
                )

        first, second = await asyncio.gather(
            register(REQUEST_1, "material-declaration:first"),
            register(REQUEST_2, "material-declaration:second"),
            return_exceptions=True,
        )
        results = (first, second)
        assert sum(isinstance(value, MaterialEvidenceReceipt) for value in results) == 1
        assert any(
            isinstance(value, MaterialEvidenceFailure) and str(value) == "stale_classification"
            for value in results
        )

        async with database.session() as session:
            new_versions = await session.scalar(
                select(func.count())
                .select_from(ProductClassificationVersion)
                .where(
                    ProductClassificationVersion.tenant_id == TENANT_ID,
                    ProductClassificationVersion.product_id == PRODUCT_ID,
                    ProductClassificationVersion.version > 1,
                )
            )
            evidence_rows = await session.scalar(
                select(func.count()).select_from(MaterialEvidenceRecord)
            )
            copied_assets = await session.scalar(
                select(func.count())
                .select_from(ProductClassificationAsset)
                .where(
                    ProductClassificationAsset.classification_version_id != BASE_CLASSIFICATION_ID
                )
            )
        assert new_versions == 1
        assert evidence_rows == 1
        assert copied_assets == 1


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_revocation_appends_insufficient_version_and_preserves_history() -> None:
    async with _seeded_database() as database:
        registered = await _register(database)
        command = MaterialEvidenceRevocationCommand(
            request_id=REQUEST_2,
            evidence_id=registered.evidence.evidence_id,
            reason_code="merchant_withdrawn",
            reason_summary="商家撤回原声明",
        )
        async with database.session() as session:
            revoked = await MaterialEvidenceRepository(session, TENANT_ID).revoke(
                command,
                actor_context(),
            )

        assert revoked.classification_version == registered.classification_version + 1
        assert revoked.material_claim_basis == "information_insufficient"
        async with database.session() as session:
            evidence_rows = await session.scalar(
                select(func.count())
                .select_from(MaterialEvidenceRecord)
                .where(MaterialEvidenceRecord.id == registered.evidence.evidence_id)
            )
            revocation_rows = await session.scalar(
                select(func.count())
                .select_from(MaterialEvidenceRevocationRecord)
                .where(
                    MaterialEvidenceRevocationRecord.evidence_id == registered.evidence.evidence_id
                )
            )
            latest = (
                await session.execute(
                    select(ProductClassificationVersion)
                    .where(ProductClassificationVersion.product_id == PRODUCT_ID)
                    .order_by(ProductClassificationVersion.version.desc())
                    .limit(1)
                )
            ).scalar_one()
        assert evidence_rows == 1
        assert revocation_rows == 1
        assert latest.material_claim_basis == "information_insufficient"
        assert latest.material_evidence_id is None


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_revocation_replay_is_stable_but_changed_reason_conflicts() -> None:
    async with _seeded_database() as database:
        registered = await _register(database)
        command = MaterialEvidenceRevocationCommand(
            request_id=REQUEST_2,
            evidence_id=registered.evidence.evidence_id,
            reason_code="merchant_withdrawn",
            reason_summary="商家撤回原声明",
        )
        async with database.session() as session:
            first = await MaterialEvidenceRepository(session, TENANT_ID).revoke(
                command,
                actor_context(),
            )
        async with database.session() as session:
            replay = await MaterialEvidenceRepository(session, TENANT_ID).revoke(
                command,
                actor_context(),
            )
        assert replay == first

        changed = command.model_copy(update={"reason_code": "wrong_product"})
        async with database.session() as session:
            with pytest.raises(
                MaterialEvidenceFailure,
                match="material_evidence_request_conflict",
            ):
                await MaterialEvidenceRepository(session, TENANT_ID).revoke(
                    changed,
                    actor_context(),
                )


@pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires the local isolated PostgreSQL acceptance service",
)
async def test_keep_insufficient_reuses_current_and_cannot_hide_active_evidence() -> None:
    async with _seeded_database() as database:
        first_command = MaterialEvidenceInsufficientCommand(
            request_id=REQUEST_1,
            product_code="BCNHDIY914",
            base_classification_id=BASE_CLASSIFICATION_ID,
        )
        async with database.session() as session:
            first = await MaterialEvidenceRepository(
                session,
                TENANT_ID,
            ).keep_insufficient(first_command)
        async with database.session() as session:
            second = await MaterialEvidenceRepository(
                session,
                TENANT_ID,
            ).keep_insufficient(first_command.model_copy(update={"request_id": REQUEST_2}))
        assert second.classification_id == first.classification_id
        assert second.classification_version == first.classification_version

        registered = await _register(database, REQUEST_3, "material-declaration:active")
        active_command = MaterialEvidenceInsufficientCommand(
            request_id=REQUEST_4,
            product_code="BCNHDIY914",
            base_classification_id=registered.classification_id,
        )
        async with database.session() as session:
            with pytest.raises(
                MaterialEvidenceFailure,
                match="material_evidence_revocation_required",
            ):
                await MaterialEvidenceRepository(
                    session,
                    TENANT_ID,
                ).keep_insufficient(active_command)
