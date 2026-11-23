import asyncio
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.contracts import ConfirmedSnapshotBatch
from photo_organizer.domain.enums import AssetStatus, TaskStatus
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.cleanup_job import StorageCleanupJob
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.review import ClassificationReviewItem
from photo_organizer.models.task import ImportTask
from photo_organizer.models.upload_binding import ControlledUploadBinding
from photo_organizer.models.user import User

pytestmark = pytest.mark.integration

SNAPSHOT_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "contracts/fixtures/first-10-sku-categorization-events.json"
)


def test_alembic_accepts_plain_postgresql_url() -> None:
    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"]

    result = subprocess.run(
        [str(backend / ".venv/bin/alembic"), "current"],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_alembic_schema_matches_model_metadata() -> None:
    """A migration-only index must not drift away from SQLAlchemy metadata."""
    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )

    result = await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "check"],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_empty_album_product_snapshot_migration_roundtrip(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty 0012 schema can be removed and recreated with its index."""
    async with auth_session_factory() as session:
        await session.execute(delete(OpcEvidenceAsset))
        await session.execute(delete(AlbumProductSnapshot))
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    command_env = {**os.environ, "DATABASE_URL": database_url}
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "downgrade", "0011_opc_memberships"],
        cwd=backend,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        async with auth_session_factory() as session:
            downgraded_revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            removed_table = await session.scalar(
                text("SELECT to_regclass('album_product_snapshots')")
            )
        assert downgraded_revision == "0011_opc_memberships"
        assert removed_table is None
    finally:
        upgrade = await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert upgrade.returncode == 0, upgrade.stderr

    async with auth_session_factory() as session:
        restored_revision = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        restored_table = await session.scalar(
            text("SELECT to_regclass('album_product_snapshots')")
        )
        restored_index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = "
                "'ix_album_product_snapshots_opc_confirmation'"
            )
        )
        manifest_index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = "
                "'ix_product_classifications_opc_confirmed_manifest'"
            )
        )
    assert restored_revision == "0019_image_set_publication"
    assert restored_table == "album_product_snapshots"
    assert restored_index is not None
    assert "group_confirmed_at DESC" in restored_index
    assert "id DESC" in restored_index
    assert manifest_index is not None
    assert "confirmed_at DESC" in manifest_index
    assert "id DESC" in manifest_index
    assert "WHERE" in manifest_index
    assert "status" in manifest_index
    assert "'confirmed'" in manifest_index


@pytest.mark.asyncio
async def test_empty_evidence_asset_migration_roundtrip(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty 0013 registry can be removed and recreated with its constraints."""
    async with auth_session_factory() as session:
        await session.execute(delete(OpcEvidenceAsset))
        await session.execute(delete(AlbumProductSnapshot))
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    command_env = {**os.environ, "DATABASE_URL": database_url}
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [
            str(backend / ".venv/bin/alembic"),
            "downgrade",
            "0012_album_product_snapshots",
        ],
        cwd=backend,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        async with auth_session_factory() as session:
            revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            removed = await session.scalar(
                text("SELECT to_regclass('opc_evidence_assets')")
            )
        assert revision == "0012_album_product_snapshots"
        assert removed is None
    finally:
        upgrade = await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert upgrade.returncode == 0, upgrade.stderr

    async with auth_session_factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        restored = await session.scalar(
            text("SELECT to_regclass('opc_evidence_assets')")
        )
        source_asset_index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'opc_evidence_assets_source_asset_key'"
            )
        )
        storage_ref_index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'opc_evidence_assets_storage_ref_key'"
            )
        )
    assert revision == "0019_image_set_publication"
    assert restored == "opc_evidence_assets"
    assert source_asset_index is not None and "UNIQUE" in source_asset_index
    assert storage_ref_index is not None and "UNIQUE" in storage_ref_index


@pytest.mark.asyncio
async def test_snapshot_evidence_mode_backfills_legacy_and_defaults_new_rows(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only pre-cutover snapshots retain direct logical-key resolution."""
    async with auth_session_factory() as session:
        await session.execute(delete(AlbumProductSnapshot))
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    command_env = {**os.environ, "DATABASE_URL": database_url}
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "downgrade", "0013_opc_evidence_assets"],
        cwd=backend,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
    )
    legacy_id = uuid4()
    current_id = uuid4()
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        async with auth_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO album_product_snapshots ("
                    "source_event_id, opc_id, album_product_id, snapshot_version, "
                    "source_import_task_id, source_type, occurred_at, "
                    "group_confirmed_by, group_confirmed_at, consent_basis, "
                    "allowed_consumers, images, source_payload, id"
                    ") VALUES ("
                    ":event_id, 'migration-opc', 'legacy-product', 1, "
                    "'migration-task', 'legacy_material', now(), "
                    "'migration-user', now(), 'opc_owned_product_material', "
                    "'[]'::json, '[]'::json, '{}'::json, :id"
                    ")"
                ),
                {"event_id": uuid4(), "id": legacy_id},
            )
            await session.commit()
        upgrade = await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert upgrade.returncode == 0, upgrade.stderr
        async with auth_session_factory() as session:
            legacy_mode = await session.scalar(
                select(AlbumProductSnapshot.evidence_resolution_mode).where(
                    AlbumProductSnapshot.id == legacy_id
                )
            )
            await session.execute(
                text(
                    "INSERT INTO album_product_snapshots ("
                    "source_event_id, opc_id, album_product_id, snapshot_version, "
                    "source_import_task_id, source_type, occurred_at, "
                    "group_confirmed_by, group_confirmed_at, consent_basis, "
                    "allowed_consumers, images, source_payload, id"
                    ") VALUES ("
                    ":event_id, 'migration-opc', 'current-product', 1, "
                    "'migration-task', 'first_sale_bead', now(), "
                    "'migration-user', now(), 'opc_owned_product_material', "
                    "'[]'::json, '[]'::json, '{}'::json, :id"
                    ")"
                ),
                {"event_id": uuid4(), "id": current_id},
            )
            await session.commit()
            current_mode = await session.scalar(
                select(AlbumProductSnapshot.evidence_resolution_mode).where(
                    AlbumProductSnapshot.id == current_id
                )
            )
        assert legacy_mode == "legacy_direct"
        assert current_mode == "registry_required"
    finally:
        restore = await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert restore.returncode == 0, restore.stderr
        async with auth_session_factory() as session:
            await session.execute(
                delete(AlbumProductSnapshot).where(
                    AlbumProductSnapshot.id.in_((legacy_id, current_id))
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_evidence_asset_downgrade_refuses_registered_original(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A migration must never silently discard an OPC evidence mapping."""
    user = User(id=uuid4(), phone_e164="+8613800019099")
    workspace = OpcWorkspace(id="opc-evidence-migration", display_name="Evidence OPC")
    task = ImportTask(
        id=uuid4(),
        user_id=user.id,
        name="Evidence migration",
        status=TaskStatus.UPLOADING,
    )
    blob = StoredBlob(
        id=uuid4(),
        user_id=user.id,
        sha256="a" * 64,
        byte_size=12,
        object_key=f"private/{user.id}/original",
        is_ready=True,
    )
    asset = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        blob_id=blob.id,
        local_asset_id="migration-original",
        filename="migration.jpg",
        media_type="image/jpeg",
        byte_size=blob.byte_size,
        sha256=blob.sha256,
        status=AssetStatus.UPLOADED,
    )
    evidence = OpcEvidenceAsset(
        id=uuid4(),
        opc_id=workspace.id,
        source_asset_id=asset.id,
        source_blob_id=blob.id,
        created_by=user.id,
        storage_ref="",
        source_object_key=blob.object_key,
        sha256=blob.sha256,
        byte_size=blob.byte_size,
        media_type="image/jpeg",
        captured_at=None,
        imported_at=asset.created_at,
    )
    evidence.storage_ref = f"evidence/{evidence.id}/original"
    async with auth_session_factory() as session:
        session.add_all([user, workspace])
        await session.flush()
        session.add(OpcMembership(opc_id=workspace.id, user_id=user.id, role="owner"))
        session.add_all([task, blob])
        await session.flush()
        session.add(asset)
        await session.flush()
        evidence.imported_at = asset.created_at
        session.add(evidence)
        await session.commit()
        version_before = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            str(backend / ".venv/bin/alembic"),
            "downgrade",
            "0012_album_product_snapshots",
        ],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "opc_evidence_assets contains data" in result.stderr
    async with auth_session_factory() as session:
        retained = await session.get(OpcEvidenceAsset, evidence.id)
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        await session.execute(
            delete(OpcEvidenceAsset).where(OpcEvidenceAsset.id == evidence.id)
        )
        await session.execute(
            delete(OpcMembership).where(OpcMembership.opc_id == workspace.id)
        )
        await session.execute(
            delete(OpcWorkspace).where(OpcWorkspace.id == workspace.id)
        )
        await session.commit()
    assert retained is not None
    assert revision == version_before == "0019_image_set_publication"


@pytest.mark.asyncio
async def test_data_bearing_downgrade_refuses_inflight_digest_duplicates_without_mutation(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The partial-index schema must not be downgraded into silent data loss."""
    user = User(id=uuid4(), phone_e164="+8613800019001")
    blobs = [
        StoredBlob(
            id=uuid4(),
            user_id=user.id,
            sha256="f" * 64,
            byte_size=8,
            object_key=f"private/downgrade-{number}",
            is_ready=False,
        )
        for number in range(2)
    ]
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add_all(blobs)
        await session.commit()
        version_before = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "downgrade", "0002_otp_delivery_state"],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "duplicate stored_blobs" in result.stderr
    async with auth_session_factory() as session:
        retained = list(
            (
                await session.scalars(
                    select(StoredBlob).where(StoredBlob.user_id == user.id)
                )
            ).all()
        )
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert {blob.id for blob in retained} == {blob.id for blob in blobs}
    assert version == version_before


@pytest.mark.asyncio
async def test_classification_review_downgrade_refuses_confirmed_history(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    classification = ProductClassification(
        opc_id="synthetic-opc-migration",
        album_product_id="album-product-migration",
        source_type="legacy_material",
        input_event_id=uuid4(),
        candidate_event_id=uuid4(),
        input_snapshot_version=1,
        classification_version=1,
        engine_name="migration-fixture",
        engine_version="1.0",
        candidates=[
            {
                "rank": 1,
                "level_1": "其他",
                "level_2": "其他",
                "confidence": 0.5,
                "reason_codes": ["insufficient_evidence"],
            }
        ],
    )
    async with auth_session_factory() as session:
        session.add(classification)
        await session.flush()
        session.add(
            ClassificationReviewItem(
                opc_id=classification.opc_id,
                classification_id=classification.id,
            )
        )
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            str(backend / ".venv/bin/alembic"),
            "downgrade",
            "0009_categorization_deliveries",
        ],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "classification review history exists" in result.stderr
    async with auth_session_factory() as session:
        retained = await session.get(ProductClassification, classification.id)
        await session.execute(
            delete(ClassificationReviewItem).where(
                ClassificationReviewItem.classification_id == classification.id
            )
        )
        await session.execute(
            delete(ProductClassification).where(
                ProductClassification.id == classification.id
            )
        )
        await session.commit()
    assert retained is not None


@pytest.mark.asyncio
async def test_opc_membership_downgrade_refuses_authority_data(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dropping configured tenant authority must never be an implicit downgrade."""
    user = User(id=uuid4(), phone_e164="+8613800019011")
    workspace = OpcWorkspace(id="opc-migration", display_name="Migration OPC")
    membership = OpcMembership(
        opc_id=workspace.id,
        user_id=user.id,
        role="owner",
    )
    async with auth_session_factory() as session:
        session.add_all([user, workspace])
        await session.flush()
        session.add(membership)
        await session.commit()
        version_before = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            str(backend / ".venv/bin/alembic"),
            "downgrade",
            "0010_classification_reviews",
        ],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "OPC authority data exists" in result.stderr
    async with auth_session_factory() as session:
        retained = await session.get(OpcMembership, membership.id)
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        await session.execute(delete(OpcMembership))
        await session.execute(delete(OpcWorkspace))
        await session.commit()
    assert retained is not None
    assert version == version_before == "0019_image_set_publication"


@pytest.mark.asyncio
async def test_album_product_snapshot_downgrade_refuses_evidence_data(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A downgrade must not silently discard immutable product image evidence."""
    source = ConfirmedSnapshotBatch.model_validate_json(
        SNAPSHOT_FIXTURE.read_text(encoding="utf-8")
    ).events[0]
    snapshot = AlbumProductSnapshot(
        source_event_id=source.event_id,
        opc_id=source.opc_id,
        album_product_id=source.album_product_id,
        snapshot_version=source.snapshot_version,
        source_import_task_id=source.source_import_task_id,
        source_type=source.source_type.value,
        occurred_at=source.occurred_at,
        group_confirmed_by=source.human_confirmation.confirmed_by,
        group_confirmed_at=source.human_confirmation.confirmed_at,
        consent_basis=source.consent.basis,
        allowed_consumers=list(source.allowed_consumers),
        images=[image.model_dump(mode="json") for image in source.images],
        source_payload=source.model_dump(mode="json"),
    )
    async with auth_session_factory() as session:
        await session.execute(delete(AlbumProductSnapshot))
        session.add(snapshot)
        await session.commit()
        version_before = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "downgrade", "0011_opc_memberships"],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )
    await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "album_product_snapshots contains evidence resolution data" in result.stderr
    async with auth_session_factory() as session:
        retained = await session.get(AlbumProductSnapshot, snapshot.id)
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        if retained is not None:
            await session.delete(retained)
            await session.commit()
    assert retained is not None
    assert version == version_before == "0019_image_set_publication"


@pytest.mark.asyncio
async def test_postgres_rejects_invalid_cleanup_claim_state_and_negative_generations(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cleanup ownership and fencing counters are database invariants, not service conventions."""
    user = User(id=uuid4(), phone_e164="+8613800019002")
    user_id = user.id
    blob = StoredBlob(
        id=uuid4(),
        user_id=user_id,
        sha256="e" * 64,
        byte_size=8,
        object_key="private/constraint",
        initialization_generation=-1,
    )
    async with auth_session_factory() as session:
        session.add(user)
        await session.commit()
        session.add(blob)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    valid_blob = StoredBlob(
        id=uuid4(),
        user_id=user_id,
        sha256="e" * 64,
        byte_size=8,
        object_key="private/constraint-valid",
    )
    async with auth_session_factory() as session:
        session.add(valid_blob)
        await session.commit()
    async with auth_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "UPDATE stored_blobs SET completion_generation = -1 WHERE id = :id"
                ),
                {"id": valid_blob.id},
            )
        await session.rollback()

    invalid_jobs = [
        {"status": "pending", "claim_id": "unexpected"},
        {"status": "processing"},
        {"status": "dead"},
        {"status": "dead", "terminal_code": "safe", "claim_generation": -1},
        {"status": "pending", "attempts": -1},
    ]
    for values in invalid_jobs:
        async with auth_session_factory() as session:
            session.add(
                StorageCleanupJob(
                    user_id=user_id,
                    blob_id=valid_blob.id,
                    operation="delete_object",
                    object_key=f"{valid_blob.object_key}/{uuid4()}",
                    **values,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


@pytest.mark.asyncio
async def test_authenticated_upload_binding_enforces_task_opc_and_user(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing either composite FK must permit an authority-crossing row."""
    workspace_suffix = uuid4().hex
    user = User(phone_e164="+8613800019010")
    other_user = User(phone_e164="+8613800019011")
    workspace = OpcWorkspace(
        id=f"opc-binding-a-{workspace_suffix}", display_name="Binding A"
    )
    other_workspace = OpcWorkspace(
        id=f"opc-binding-b-{workspace_suffix}", display_name="Binding B"
    )
    async with auth_session_factory() as session:
        session.add_all([user, other_user, workspace, other_workspace])
        await session.flush()
        task = ImportTask(
            user_id=user.id,
            opc_id=workspace.id,
            name="Authenticated binding fixture",
        )
        session.add(task)
        await session.flush()
        binding = ControlledUploadBinding(
            candidate_id=f"opc-controlled-upload-candidate:{'1' * 64}",
            source_readiness_id=f"opc-real-sku-pilot-readiness:{'2' * 64}",
            opc_id=workspace.id,
            task_id=task.id,
            user_id=user.id,
            album_product_id="album-product-binding-001",
            source_type="first_sale_bead",
            item_count=2,
        )
        session.add(binding)
        await session.commit()

    async with auth_session_factory() as session:
        session.add(
            ControlledUploadBinding(
                candidate_id=f"opc-controlled-upload-candidate:{'3' * 64}",
                source_readiness_id=f"opc-real-sku-pilot-readiness:{'4' * 64}",
                opc_id=other_workspace.id,
                task_id=task.id,
                user_id=user.id,
                album_product_id="album-product-binding-002",
                source_type="first_sale_bead",
                item_count=2,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with auth_session_factory() as session:
        session.add(
            ControlledUploadBinding(
                candidate_id=f"opc-controlled-upload-candidate:{'5' * 64}",
                source_readiness_id=f"opc-real-sku-pilot-readiness:{'6' * 64}",
                opc_id=workspace.id,
                task_id=task.id,
                user_id=other_user.id,
                album_product_id="album-product-binding-003",
                source_type="first_sale_bead",
                item_count=2,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with auth_session_factory() as session:
        await session.execute(
            delete(ControlledUploadBinding).where(
                ControlledUploadBinding.task_id == task.id
            )
        )
        await session.execute(delete(ImportTask).where(ImportTask.id == task.id))
        await session.execute(
            delete(OpcWorkspace).where(
                OpcWorkspace.id.in_((workspace.id, other_workspace.id))
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_empty_authenticated_upload_binding_migration_roundtrip(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty 0015 schema must downgrade and recreate its authority constraints."""
    async with auth_session_factory() as session:
        await session.execute(delete(ControlledUploadBinding))
        await session.execute(delete(ImportTask))
        await session.execute(delete(OpcMembership))
        await session.execute(delete(OpcWorkspace))
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    command_env = {**os.environ, "DATABASE_URL": database_url}
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [
            str(backend / ".venv/bin/alembic"),
            "downgrade",
            "0014_snapshot_evidence_mode",
        ],
        cwd=backend,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        async with auth_session_factory() as session:
            revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            binding_table = await session.scalar(
                text("SELECT to_regclass('controlled_upload_bindings')")
            )
            task_opc_column = await session.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='import_tasks' AND column_name='opc_id'"
                )
            )
        assert revision == "0014_snapshot_evidence_mode"
        assert binding_table is None
        assert task_opc_column is None
    finally:
        upgrade = await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert upgrade.returncode == 0, upgrade.stderr

    async with auth_session_factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        binding_table = await session.scalar(
            text("SELECT to_regclass('controlled_upload_bindings')")
        )
        task_opc_column = await session.scalar(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='import_tasks' AND column_name='opc_id'"
            )
        )
    assert revision == "0019_image_set_publication"
    assert binding_table == "controlled_upload_bindings"
    assert task_opc_column == "opc_id"


@pytest.mark.asyncio
async def test_empty_upload_binding_authority_migration_roundtrip(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An empty 0016 authority column can be removed and recreated exactly."""
    async with auth_session_factory() as session:
        await session.execute(delete(ControlledUploadBinding))
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    command_env = {**os.environ, "DATABASE_URL": database_url}
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "downgrade", "0015_upload_bindings"],
        cwd=backend,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        async with auth_session_factory() as session:
            revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            authority_column = await session.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='controlled_upload_bindings' "
                    "AND column_name='candidate_payload'"
                )
            )
        assert revision == "0015_upload_bindings"
        assert authority_column is None
    finally:
        upgrade = await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert upgrade.returncode == 0, upgrade.stderr

    async with auth_session_factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        authority_column = await session.scalar(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='controlled_upload_bindings' "
                "AND column_name='candidate_payload'"
            )
        )
    assert revision == "0019_image_set_publication"
    assert authority_column == "candidate_payload"


@pytest.mark.asyncio
async def test_upload_binding_authority_downgrade_refuses_candidate_snapshots(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dropping the per-image authority snapshot must fail without data loss."""
    suffix = uuid4().hex
    user = User(phone_e164=f"+86138{str(uuid4().int)[-8:]}")
    workspace = OpcWorkspace(
        id=f"opc-binding-authority-{suffix}",
        display_name="Binding Authority Downgrade",
    )
    async with auth_session_factory() as session:
        session.add_all([user, workspace])
        await session.flush()
        task = ImportTask(
            user_id=user.id,
            opc_id=workspace.id,
            name="Binding authority downgrade fixture",
        )
        session.add(task)
        await session.flush()
        binding = ControlledUploadBinding(
            candidate_id=f"opc-controlled-upload-candidate:{'b' * 64}",
            source_readiness_id=f"opc-real-sku-pilot-readiness:{'c' * 64}",
            opc_id=workspace.id,
            task_id=task.id,
            user_id=user.id,
            album_product_id="album-product-binding-authority",
            source_type="first_sale_bead",
            item_count=2,
            candidate_payload={"schema_version": "authority-fixture"},
        )
        session.add(binding)
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "downgrade", "0015_upload_bindings"],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )

    assert downgrade.returncode != 0
    assert "candidate authority exists" in downgrade.stderr
    async with auth_session_factory() as session:
        retained = await session.get(ControlledUploadBinding, binding.id)
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert retained is not None
        assert retained.candidate_payload == {"schema_version": "authority-fixture"}
        await session.delete(retained)
        await session.flush()
        await session.delete(task)
        await session.flush()
        await session.delete(workspace)
        await session.delete(user)
        await session.commit()
    assert revision == "0019_image_set_publication"


@pytest.mark.asyncio
async def test_authenticated_upload_binding_downgrade_cannot_race_a_new_binding(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A committed authority row and a destructive downgrade cannot both win."""
    suffix = uuid4().hex
    user = User(phone_e164=f"+86138{str(uuid4().int)[-8:]}")
    workspace = OpcWorkspace(
        id=f"opc-binding-race-{suffix}",
        display_name="Binding Downgrade Race",
    )
    async with auth_session_factory() as session:
        session.add_all([user, workspace])
        await session.flush()
        task = ImportTask(
            user_id=user.id,
            name="Binding downgrade race fixture",
        )
        session.add(task)
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    command_env = {**os.environ, "DATABASE_URL": database_url}

    async with auth_session_factory() as writer:
        persisted_task = await writer.get(ImportTask, task.id)
        assert persisted_task is not None
        persisted_task.opc_id = workspace.id
        writer.add(
            ControlledUploadBinding(
                candidate_id=f"opc-controlled-upload-candidate:{'9' * 64}",
                source_readiness_id=(f"opc-real-sku-pilot-readiness:{'a' * 64}"),
                opc_id=workspace.id,
                task_id=task.id,
                user_id=user.id,
                album_product_id="album-product-binding-race",
                source_type="first_sale_bead",
                item_count=2,
            )
        )
        await writer.flush()
        downgrade_task = asyncio.create_task(
            asyncio.to_thread(
                subprocess.run,
                [
                    str(backend / ".venv/bin/alembic"),
                    "downgrade",
                    "0014_snapshot_evidence_mode",
                ],
                cwd=backend,
                env=command_env,
                check=False,
                capture_output=True,
                text=True,
            )
        )
        downgrade_waiting = False
        for _ in range(250):
            async with auth_session_factory() as observer:
                downgrade_waiting = bool(
                    await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND pid <> pg_backend_pid() "
                            "AND wait_event_type = 'Lock' "
                            "AND (query ILIKE '%controlled_upload_bindings%' "
                            "OR query ILIKE '%import_tasks%' "
                            "OR query ILIKE '%product_code_bindings%'))"
                        )
                    )
                )
            if downgrade_waiting:
                break
            await asyncio.sleep(0.01)
        assert downgrade_waiting, "downgrade never reached the authority lock boundary"
        await writer.commit()
        downgrade = await downgrade_task

    if downgrade.returncode == 0:
        upgrade = await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert upgrade.returncode == 0, upgrade.stderr

    async with auth_session_factory() as session:
        retained_binding = await session.scalar(
            select(ControlledUploadBinding).where(
                ControlledUploadBinding.task_id == task.id
            )
        )
        retained_task = await session.get(ImportTask, task.id)
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        if retained_binding is not None:
            await session.delete(retained_binding)
            await session.flush()
        if retained_task is not None:
            await session.delete(retained_task)
            await session.flush()
        await session.execute(
            delete(OpcWorkspace).where(OpcWorkspace.id == workspace.id)
        )
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()

    assert downgrade.returncode != 0
    assert "authenticated upload binding authority exists" in downgrade.stderr
    assert retained_binding is not None
    assert retained_task is not None and retained_task.opc_id == workspace.id
    assert revision == "0019_image_set_publication"


@pytest.mark.asyncio
async def test_authenticated_upload_binding_downgrade_refuses_authority_data(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The downgrade guard must retain task OPC and binding authority."""
    workspace_suffix = uuid4().hex
    user = User(phone_e164="+8613800019012")
    workspace = OpcWorkspace(
        id=f"opc-binding-downgrade-{workspace_suffix}",
        display_name="Binding Downgrade",
    )
    async with auth_session_factory() as session:
        session.add_all([user, workspace])
        await session.flush()
        task = ImportTask(
            user_id=user.id,
            opc_id=workspace.id,
            name="Binding downgrade fixture",
        )
        session.add(task)
        await session.flush()
        binding = ControlledUploadBinding(
            candidate_id=f"opc-controlled-upload-candidate:{'7' * 64}",
            source_readiness_id=f"opc-real-sku-pilot-readiness:{'8' * 64}",
            opc_id=workspace.id,
            task_id=task.id,
            user_id=user.id,
            album_product_id="album-product-binding-downgrade",
            source_type="first_sale_bead",
            item_count=2,
        )
        session.add(binding)
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    command_env = {**os.environ, "DATABASE_URL": database_url}
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [
            str(backend / ".venv/bin/alembic"),
            "downgrade",
            "0014_snapshot_evidence_mode",
        ],
        cwd=backend,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
    )
    if downgrade.returncode == 0:
        await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=True,
            capture_output=True,
            text=True,
        )

    assert downgrade.returncode != 0
    assert "authenticated upload binding authority exists" in downgrade.stderr
    async with auth_session_factory() as session:
        retained_binding = await session.get(ControlledUploadBinding, binding.id)
        retained_task = await session.get(ImportTask, task.id)
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        if retained_binding is not None:
            await session.execute(
                delete(ControlledUploadBinding).where(
                    ControlledUploadBinding.id == retained_binding.id
                )
            )
            await session.flush()
        if retained_task is not None:
            await session.execute(
                delete(ImportTask).where(ImportTask.id == retained_task.id)
            )
            await session.flush()
        await session.execute(
            delete(OpcWorkspace).where(OpcWorkspace.id == workspace.id)
        )
        await session.commit()
    assert retained_binding is not None
    assert retained_task is not None and retained_task.opc_id == workspace.id
    assert revision == "0019_image_set_publication"


@pytest.mark.asyncio
async def test_empty_task_duplicate_receipt_migration_roundtrip(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Canonical data survives removing and recreating the empty receipt schema."""
    async with auth_session_factory() as session:
        await session.execute(delete(Asset))
        await session.execute(delete(ImportTask))
        await session.commit()
    user = User(id=uuid4(), phone_e164=f"+8613{uuid4().int % 10**8:08d}")
    task = ImportTask(id=uuid4(), user_id=user.id, name="Canonical roundtrip")
    canonical = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        local_asset_id="canonical-roundtrip",
        filename="canonical.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="8" * 64,
        status=AssetStatus.UPLOADED,
    )
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add(task)
        await session.flush()
        session.add(canonical)
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    command_env = {**os.environ, "DATABASE_URL": database_url}
    upgrade = await asyncio.to_thread(
        subprocess.run,
        [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
        cwd=backend,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    async with auth_session_factory() as session:
        upgraded_revision = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        canonical_column = await session.scalar(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='assets' AND column_name='canonical_asset_id'"
            )
        )
        duplicate_enum = await session.scalar(
            text(
                "SELECT enumlabel FROM pg_enum JOIN pg_type "
                "ON pg_enum.enumtypid=pg_type.oid "
                "WHERE pg_type.typname='asset_status' "
                "AND enumlabel='skipped_duplicate'"
            )
        )
    assert upgraded_revision == "0019_image_set_publication"
    assert canonical_column == "canonical_asset_id"
    assert duplicate_enum == "skipped_duplicate"

    downgrade = await asyncio.to_thread(
        subprocess.run,
        [
            str(backend / ".venv/bin/alembic"),
            "downgrade",
            "0016_upload_binding_authority",
        ],
        cwd=backend,
        env=command_env,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        assert downgrade.returncode == 0, downgrade.stderr
        async with auth_session_factory() as session:
            downgraded_revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            canonical_column = await session.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='assets' AND column_name='canonical_asset_id'"
                )
            )
            duplicate_enum = await session.scalar(
                text(
                    "SELECT enumlabel FROM pg_enum JOIN pg_type "
                    "ON pg_enum.enumtypid=pg_type.oid "
                    "WHERE pg_type.typname='asset_status' "
                    "AND enumlabel='skipped_duplicate'"
                )
            )
            preserved = (
                (
                    await session.execute(
                        text(
                            "SELECT local_asset_id, sha256 FROM assets WHERE id=:asset_id"
                        ),
                        {"asset_id": canonical.id},
                    )
                )
                .mappings()
                .one()
            )
        assert downgraded_revision == "0016_upload_binding_authority"
        assert canonical_column is None
        assert duplicate_enum is None
        assert preserved == {
            "local_asset_id": canonical.local_asset_id,
            "sha256": canonical.sha256,
        }
    finally:
        restore = await asyncio.to_thread(
            subprocess.run,
            [str(backend / ".venv/bin/alembic"), "upgrade", "head"],
            cwd=backend,
            env=command_env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert restore.returncode == 0, restore.stderr
    async with auth_session_factory() as session:
        restored = await session.get(Asset, canonical.id)
        if restored is not None:
            await session.delete(restored)
            await session.flush()
        restored_task = await session.get(ImportTask, task.id)
        if restored_task is not None:
            await session.delete(restored_task)
            await session.flush()
        restored_user = await session.get(User, user.id)
        if restored_user is not None:
            await session.delete(restored_user)
        await session.commit()
    assert restored is not None
    assert restored.sha256 == canonical.sha256


@pytest.mark.asyncio
async def test_duplicate_receipt_downgrade_refuses_data_without_mutation(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing the receipt schema may not silently discard duplicate provenance."""
    async with auth_session_factory() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        canonical_column = await session.scalar(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='assets' AND column_name='canonical_asset_id'"
            )
        )
    assert revision == "0019_image_set_publication"
    assert canonical_column == "canonical_asset_id"

    user = User(id=uuid4(), phone_e164=f"+8613{uuid4().int % 10**8:08d}")
    task = ImportTask(id=uuid4(), user_id=user.id, name="Duplicate downgrade")
    canonical = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        local_asset_id="canonical",
        filename="same.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="9" * 64,
        status=AssetStatus.UPLOADED,
    )
    duplicate = Asset(
        id=uuid4(),
        user_id=user.id,
        task_id=task.id,
        local_asset_id="duplicate",
        filename="same-copy.jpg",
        media_type="image/jpeg",
        byte_size=8,
        sha256="9" * 64,
        status=AssetStatus.SKIPPED_DUPLICATE,
        canonical_asset_id=canonical.id,
    )
    async with auth_session_factory() as session:
        session.add(user)
        await session.flush()
        session.add(task)
        await session.flush()
        session.add_all([canonical, duplicate])
        await session.commit()

    backend = Path(__file__).resolve().parents[2]
    database_url = os.environ["PHOTO_ORGANIZER_TEST_DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    downgrade = await asyncio.to_thread(
        subprocess.run,
        [
            str(backend / ".venv/bin/alembic"),
            "downgrade",
            "0016_upload_binding_authority",
        ],
        cwd=backend,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )

    assert downgrade.returncode != 0
    assert "duplicate receipt provenance exists" in downgrade.stderr
    async with auth_session_factory() as session:
        retained = await session.get(Asset, duplicate.id)
        revision_after = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    assert retained is not None
    assert retained.canonical_asset_id == canonical.id
    assert revision_after == "0019_image_set_publication"
