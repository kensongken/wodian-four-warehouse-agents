from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.models.handoff_authority import (
    OpcExternalTenantBindingRecord,
    PreparedImageHandoffRecord,
    ProductCodeBindingRecord,
)
from photo_organizer.models.opc import OpcWorkspace
from photo_organizer.models.product_image_set_delivery import (
    ProductImageSetDelivery,
    ProductImageSetDeliveryImage,
)

pytestmark = pytest.mark.integration


async def _empty_handoff_tables(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await session.execute(delete(ProductImageSetDeliveryImage))
        await session.execute(delete(ProductImageSetDelivery))
        await session.execute(delete(PreparedImageHandoffRecord))
        await session.execute(delete(ProductCodeBindingRecord))
        await session.execute(delete(OpcExternalTenantBindingRecord))
        await session.commit()


def _alembic(target: str, database_url: str) -> subprocess.CompletedProcess[str]:
    backend = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [str(backend / ".venv/bin/alembic"), *target.split()],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_0018_real_postgres_roundtrip_and_evidence_guard(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert len("0018_image_set_delivery") <= 32
    await _empty_handoff_tables(auth_session_factory)
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"]
    downgrade = await asyncio.to_thread(
        _alembic, "downgrade 0017_task_duplicate_receipts", database_url
    )
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        upgrade = await asyncio.to_thread(_alembic, "upgrade head", database_url)
        assert upgrade.returncode == 0, upgrade.stderr
        async with auth_session_factory() as session:
            revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            tables = {
                name: await session.scalar(text(f"SELECT to_regclass('{name}')"))
                for name in (
                    "opc_external_tenant_bindings",
                    "product_code_bindings",
                    "prepared_image_handoffs",
                    "product_image_set_deliveries",
                    "product_image_set_delivery_images",
                )
            }
        assert revision == "0019_image_set_publication"
        assert all(value is not None for value in tables.values())

        async with auth_session_factory() as session:
            # A dangling authority row cannot exist: the FK must be real.
            session.add(
                OpcExternalTenantBindingRecord(
                    opc_id="missing-opc",
                    tenant_id=uuid4(),
                    binding_source="operator_provisioned",
                    provisioned_by="synthetic-operator",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

        guard_opc = "synthetic-migration-guard-opc"
        async with auth_session_factory() as session:
            session.add(OpcWorkspace(id=guard_opc, display_name="Synthetic Guard"))
            await session.flush()
            authority = OpcExternalTenantBindingRecord(
                opc_id=guard_opc,
                tenant_id=uuid4(),
                binding_source="operator_provisioned",
                provisioned_by="synthetic-operator",
            )
            session.add(authority)
            await session.commit()

        async with auth_session_factory() as session:
            with pytest.raises(
                DBAPIError, match="handoff authority records are immutable"
            ):
                await session.execute(
                    update(OpcExternalTenantBindingRecord)
                    .where(OpcExternalTenantBindingRecord.id == authority.id)
                    .values(tenant_id=uuid4())
                )
                await session.commit()
            await session.rollback()

        # Keep a RowExclusive lock and an uncommitted authority insert while the
        # downgrade waits for its ACCESS EXCLUSIVE lock. Once committed, the
        # downgrade must see the evidence and refuse to drop any table.
        competing_opc = "synthetic-migration-race-opc"
        async with auth_session_factory() as writer:
            writer.add(OpcWorkspace(id=competing_opc, display_name="Synthetic Race"))
            await writer.flush()
            writer.add(
                OpcExternalTenantBindingRecord(
                    opc_id=competing_opc,
                    tenant_id=uuid4(),
                    binding_source="operator_provisioned",
                    provisioned_by="synthetic-operator",
                )
            )
            await writer.flush()
            guarded_task = asyncio.create_task(
                asyncio.to_thread(
                    _alembic,
                    "downgrade 0017_task_duplicate_receipts",
                    database_url,
                )
            )
            for _ in range(100):
                async with auth_session_factory() as observer:
                    waiting = await observer.scalar(
                        text(
                            "SELECT EXISTS ("
                            "SELECT 1 FROM pg_locks "
                            "WHERE mode = 'AccessExclusiveLock' AND NOT granted)"
                        )
                    )
                if waiting:
                    break
                await asyncio.sleep(0.02)
            assert waiting is True
            await writer.commit()
        guarded = await guarded_task
        assert guarded.returncode != 0
        assert "cannot downgrade 0018_image_set_delivery" in (
            guarded.stdout + guarded.stderr
        )
        async with auth_session_factory() as session:
            revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            assert revision == "0019_image_set_publication"
            await session.execute(delete(OpcExternalTenantBindingRecord))
            await session.execute(
                delete(OpcWorkspace).where(
                    OpcWorkspace.id.in_((guard_opc, competing_opc))
                )
            )
            await session.commit()
    finally:
        final_upgrade = await asyncio.to_thread(_alembic, "upgrade head", database_url)
        assert final_upgrade.returncode == 0, final_upgrade.stderr
        await _empty_handoff_tables(auth_session_factory)
