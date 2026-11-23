from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.domain.enums import AssetStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.repositories.unit_of_work import UnitOfWork

ALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/heic", "image/heif"})
MAX_EVIDENCE_BYTES = 104_857_600


class EvidenceAssetRegistrationError(Exception):
    """Base class for stable evidence-registration failures."""


class EvidenceAssetNotFound(EvidenceAssetRegistrationError):
    """The source upload is absent or cannot enter the requested OPC."""


class EvidenceAssetConflict(EvidenceAssetRegistrationError):
    """An existing evidence identity no longer matches immutable metadata."""


class EvidenceAssetRegistrationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def register(
        self,
        *,
        opc_id: str,
        user_id: UUID,
        source_asset_id: UUID,
    ) -> OpcEvidenceAsset:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"opc-evidence-asset:{source_asset_id}"},
            )
            existing = await session.scalar(
                select(OpcEvidenceAsset)
                .where(OpcEvidenceAsset.source_asset_id == source_asset_id)
                .with_for_update()
            )
            if existing is not None and (
                existing.opc_id != opc_id or existing.created_by != user_id
            ):
                raise EvidenceAssetNotFound("asset_not_found")

            source = await self._source(
                session,
                source_asset_id=source_asset_id,
            )
            if source is None:
                if existing is not None:
                    raise EvidenceAssetConflict("evidence_asset_conflict")
                raise EvidenceAssetNotFound("asset_not_found")
            asset, blob = source
            if asset.user_id != user_id or blob.user_id != user_id:
                raise EvidenceAssetNotFound("asset_not_found")

            if existing is not None:
                if not self._is_eligible(asset, blob) or not self._matches(
                    existing,
                    opc_id=opc_id,
                    user_id=user_id,
                    asset=asset,
                    blob=blob,
                ):
                    raise EvidenceAssetConflict("evidence_asset_conflict")
                return existing

            if not self._is_eligible(asset, blob):
                raise EvidenceAssetNotFound("asset_not_found")

            evidence_id = uuid4()
            evidence = OpcEvidenceAsset(
                id=evidence_id,
                opc_id=opc_id,
                source_asset_id=asset.id,
                source_blob_id=blob.id,
                created_by=user_id,
                storage_ref=f"evidence/{evidence_id}/original",
                source_object_key=blob.object_key,
                sha256=blob.sha256,
                byte_size=blob.byte_size,
                media_type=asset.media_type,
                captured_at=asset.captured_at,
                imported_at=asset.created_at,
            )
            session.add(evidence)
            await session.flush()
            return evidence

    async def _source(
        self,
        session: AsyncSession,
        *,
        source_asset_id: UUID,
    ) -> tuple[Asset, StoredBlob] | None:
        asset = await session.scalar(
            select(Asset).where(Asset.id == source_asset_id).with_for_update()
        )
        if asset is None or asset.blob_id is None:
            return None
        blob = await session.scalar(
            select(StoredBlob).where(StoredBlob.id == asset.blob_id).with_for_update()
        )
        if blob is None:
            return None
        return asset, blob

    @staticmethod
    def _is_eligible(asset: Asset, blob: StoredBlob) -> bool:
        return (
            asset.status is AssetStatus.UPLOADED
            and blob.is_ready
            and asset.sha256 is not None
            and asset.sha256 == blob.sha256
            and asset.byte_size is not None
            and asset.byte_size == blob.byte_size
            and 1 <= asset.byte_size <= MAX_EVIDENCE_BYTES
            and asset.media_type in ALLOWED_MEDIA_TYPES
            and bool(blob.object_key)
        )

    @staticmethod
    def _matches(
        evidence: OpcEvidenceAsset,
        *,
        opc_id: str,
        user_id: UUID,
        asset: Asset,
        blob: StoredBlob,
    ) -> bool:
        return (
            evidence.opc_id == opc_id
            and evidence.created_by == user_id
            and evidence.source_asset_id == asset.id
            and evidence.source_blob_id == blob.id
            and evidence.storage_ref == f"evidence/{evidence.id}/original"
            and evidence.source_object_key == blob.object_key
            and evidence.sha256 == asset.sha256 == blob.sha256
            and evidence.byte_size == asset.byte_size == blob.byte_size
            and evidence.media_type == asset.media_type
            and evidence.captured_at == asset.captured_at
            and evidence.imported_at == asset.created_at
        )
