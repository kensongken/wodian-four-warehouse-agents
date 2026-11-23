from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.manifest import ProductMaterialManifestService
from photo_organizer.categorization.contracts import ImageEvidence
from photo_organizer.domain.enums import AssetStatus
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.storage.object_storage import (
    ObjectStorage,
    StorageNotFound,
    StoragePermanent,
    StorageTransient,
)

DOWNLOAD_EXPIRES_SECONDS = 300


class ProductMaterialDownloadError(Exception):
    """Base class for stable product-material download failures."""


class MaterialNotFound(ProductMaterialDownloadError):
    """The requested material is absent or not authorized for this OPC."""


class MaterialTemporarilyUnavailable(ProductMaterialDownloadError):
    """The storage provider is temporarily unavailable."""


class MaterialUnavailable(ProductMaterialDownloadError):
    """The storage provider cannot serve the material safely."""


@dataclass(frozen=True)
class ProductMaterialDownload:
    download_url: str
    expires_seconds: int


class ProductMaterialDownloadService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
    ) -> None:
        self._session_factory = session_factory
        self._manifest = ProductMaterialManifestService(session_factory)
        self._storage = storage

    async def sign(
        self,
        *,
        opc_id: str,
        confirmation_event_id: UUID,
        asset_id: UUID,
    ) -> ProductMaterialDownload:
        item = await self._manifest.get_item(
            opc_id=opc_id,
            confirmation_event_id=confirmation_event_id,
        )
        if item is None:
            raise MaterialNotFound("material_not_found")
        image = next(
            (candidate for candidate in item.images if candidate.asset_id == asset_id),
            None,
        )
        if image is None:
            raise MaterialNotFound("material_not_found")
        object_key = await self._resolve_object_key(
            opc_id=opc_id,
            image=image,
            source_event_id=item.source_event_id,
        )
        try:
            download_url = await self._storage.sign_download(
                object_key,
                DOWNLOAD_EXPIRES_SECONDS,
            )
        except StorageNotFound:
            raise MaterialNotFound("material_not_found") from None
        except StorageTransient:
            raise MaterialTemporarilyUnavailable(
                "material_temporarily_unavailable"
            ) from None
        except StoragePermanent:
            raise MaterialUnavailable("material_unavailable") from None
        return ProductMaterialDownload(
            download_url=download_url,
            expires_seconds=DOWNLOAD_EXPIRES_SECONDS,
        )

    async def _resolve_object_key(
        self,
        *,
        opc_id: str,
        image: ImageEvidence,
        source_event_id: UUID,
    ) -> str:
        async with self._session_factory() as session:
            candidates = (
                await session.scalars(
                    select(OpcEvidenceAsset).where(
                        or_(
                            OpcEvidenceAsset.source_asset_id == image.asset_id,
                            OpcEvidenceAsset.storage_ref == image.storage_ref,
                        )
                    )
                )
            ).all()
            resolution_mode = await session.scalar(
                select(AlbumProductSnapshot.evidence_resolution_mode).where(
                    AlbumProductSnapshot.opc_id == opc_id,
                    AlbumProductSnapshot.source_event_id == source_event_id,
                )
            )
        if not candidates:
            if resolution_mode != "legacy_direct":
                raise MaterialNotFound("material_not_found")
            return image.storage_ref
        if len(candidates) != 1:
            raise MaterialNotFound("material_not_found")
        evidence = candidates[0]
        async with self._session_factory() as session:
            asset = await session.get(Asset, evidence.source_asset_id)
            blob = (
                await session.get(StoredBlob, evidence.source_blob_id)
                if asset is not None
                else None
            )
        expected_storage_ref = f"evidence/{evidence.id}/original"
        if (
            evidence.opc_id != opc_id
            or evidence.source_asset_id != image.asset_id
            or evidence.storage_ref != image.storage_ref
            or evidence.storage_ref != expected_storage_ref
            or asset is None
            or blob is None
            or evidence.created_by != asset.user_id
            or evidence.created_by != blob.user_id
            or evidence.source_blob_id != asset.blob_id
            or evidence.source_blob_id != blob.id
            or evidence.source_object_key != blob.object_key
            or asset.status is not AssetStatus.UPLOADED
            or not blob.is_ready
            or asset.sha256 != blob.sha256
            or asset.sha256 != evidence.sha256
            or asset.sha256 != image.sha256
            or asset.byte_size != blob.byte_size
            or asset.byte_size != evidence.byte_size
            or asset.byte_size != image.byte_size
            or asset.media_type != evidence.media_type
            or asset.media_type != image.media_type
            or asset.captured_at != evidence.captured_at
            or asset.captured_at != image.captured_at
            or asset.created_at != evidence.imported_at
            or asset.created_at != image.imported_at
            or evidence.sha256 != image.sha256
            or evidence.byte_size != image.byte_size
            or evidence.media_type != image.media_type
            or evidence.captured_at != image.captured_at
            or evidence.imported_at != image.imported_at
        ):
            raise MaterialNotFound("material_not_found")
        return evidence.source_object_key
