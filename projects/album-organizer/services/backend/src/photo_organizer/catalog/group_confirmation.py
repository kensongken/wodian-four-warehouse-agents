from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.contracts import (
    CategorizationSeed,
    ConfirmedSnapshot,
    ConfirmedSnapshotBatch,
    Consent,
    HumanConfirmation,
    ImageEvidence,
    Level1Category,
    Level2Category,
    ProductSourceType,
)
from photo_organizer.domain.enums import AssetStatus
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.handoff_authority import ProductCodeBindingRecord
from photo_organizer.models.opc import OpcMembership
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask
from photo_organizer.repositories.unit_of_work import UnitOfWork

SNAPSHOT_CONFIRMED_TOPIC: Final = "album.product.snapshot.confirmed.v1"


class EvidenceGroupConfirmationError(Exception):
    """Base class for stable evidence-group confirmation failures."""


class EvidenceGroupNotFound(EvidenceGroupConfirmationError):
    """The requested group is absent or unavailable in this OPC."""


class EvidenceGroupConflict(EvidenceGroupConfirmationError):
    """A confirmation identity was already used for different content."""


class ExistingProductNotFound(EvidenceGroupConfirmationError):
    """The target product or required authority is unavailable in this OPC."""


class OpcOwnerRequired(EvidenceGroupConfirmationError):
    """Only an OPC owner may attach a batch to an existing product."""


class SnapshotVersionConflict(EvidenceGroupConfirmationError):
    """The operator acted on a stale current snapshot version."""


class ExistingProductConfirmationConflict(EvidenceGroupConfirmationError):
    """A next-snapshot event identity conflicts with existing authority."""


@dataclass(frozen=True)
class AlbumProductGroupConfirmation:
    confirmation_event_id: UUID
    album_product_id: str
    snapshot_version: int
    opc_id: str
    evidence_asset_ids: tuple[UUID, ...]
    source_type: ProductSourceType
    confirmed_by: str
    confirmed_at: datetime


@dataclass(frozen=True)
class _ValidatedGroup:
    source_import_task_id: str
    images: tuple[ImageEvidence, ...]


class AlbumProductGroupConfirmationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def confirm(
        self,
        *,
        opc_id: str,
        user_id: UUID,
        confirmation_request_id: UUID,
        evidence_asset_ids: tuple[UUID, ...],
        source_type: ProductSourceType,
        level_1_candidate: Level1Category,
        level_2_candidate: Level2Category,
    ) -> AlbumProductGroupConfirmation:
        if not 2 <= len(evidence_asset_ids) <= 4 or len(set(evidence_asset_ids)) != len(
            evidence_asset_ids
        ):
            raise ValueError("invalid_evidence_group")
        async with UnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            membership = await session.scalar(
                select(OpcMembership).where(
                    OpcMembership.opc_id == opc_id,
                    OpcMembership.user_id == user_id,
                )
            )
            if membership is None:
                raise EvidenceGroupNotFound("evidence_group_not_found")
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"album-product-group:{confirmation_request_id}"},
            )
            group = await self._validated_group(
                session,
                opc_id=opc_id,
                evidence_asset_ids=evidence_asset_ids,
            )
            confirmed_by = f"user:{user_id}"
            album_product_id = f"album-product-{confirmation_request_id}"
            existing = await session.scalar(
                select(AlbumProductSnapshot)
                .where(AlbumProductSnapshot.source_event_id == confirmation_request_id)
                .with_for_update()
            )
            if existing is not None:
                if existing.opc_id != opc_id:
                    raise EvidenceGroupNotFound("evidence_group_not_found")
                return await self._replay(
                    session,
                    existing=existing,
                    opc_id=opc_id,
                    evidence_asset_ids=evidence_asset_ids,
                    group=group,
                    source_type=source_type,
                    level_1_candidate=level_1_candidate,
                    level_2_candidate=level_2_candidate,
                    confirmed_by=confirmed_by,
                    album_product_id=album_product_id,
                )
            if await session.get(OutboxEvent, confirmation_request_id) is not None:
                raise EvidenceGroupConflict("group_confirmation_conflict")

            now = self._clock()
            event = ConfirmedSnapshot(
                schema_version="album-product-categorization/1.0",
                event_id=confirmation_request_id,
                event_type=SNAPSHOT_CONFIRMED_TOPIC,
                occurred_at=now,
                opc_id=opc_id,
                album_product_id=album_product_id,
                snapshot_version=1,
                source_import_task_id=group.source_import_task_id,
                source_type=source_type,
                images=group.images,
                human_confirmation=HumanConfirmation(
                    status="confirmed",
                    confirmed_by=confirmed_by,
                    confirmed_at=now,
                    confirmation_scope="grouping_membership",
                ),
                categorization_input=CategorizationSeed(
                    level_1_candidate=level_1_candidate,
                    level_2_candidate=level_2_candidate,
                    candidate_source="operator_seed",
                    confidence=None,
                    assertion_scope="candidate_only",
                ),
                consent=Consent(
                    status="not_required",
                    basis="opc_owned_product_material",
                ),
                allowed_consumers=("categorization", "mall_draft_export"),
                mall_product_id=None,
                bead_id=None,
            )
            snapshot = AlbumProductSnapshot(
                source_event_id=event.event_id,
                opc_id=event.opc_id,
                album_product_id=event.album_product_id,
                snapshot_version=event.snapshot_version,
                source_import_task_id=event.source_import_task_id,
                source_type=event.source_type.value,
                occurred_at=event.occurred_at,
                group_confirmed_by=event.human_confirmation.confirmed_by,
                group_confirmed_at=event.human_confirmation.confirmed_at,
                consent_basis=event.consent.basis,
                evidence_resolution_mode="registry_required",
                allowed_consumers=list(event.allowed_consumers),
                images=[image.model_dump(mode="json") for image in event.images],
                source_payload=event.model_dump(mode="json"),
            )
            session.add(snapshot)
            await session.flush()
            batch = ConfirmedSnapshotBatch(
                schema_version="album-product-categorization/1.0",
                batch_id=event.event_id,
                generated_at=event.occurred_at,
                data_classification=("opc_owned_product_material_no_customer_data"),
                events=(event,),
            )
            session.add(
                OutboxEvent(
                    id=event.event_id,
                    topic=SNAPSHOT_CONFIRMED_TOPIC,
                    aggregate_id=snapshot.id,
                    sequence=1,
                    payload=batch.model_dump(mode="json"),
                )
            )
            await session.flush()
            return self._result(event, evidence_asset_ids)

    async def confirm_next_snapshot(
        self,
        *,
        opc_id: str,
        user_id: UUID,
        album_product_id: str,
        confirmation_request_id: UUID,
        expected_current_snapshot_version: int,
        evidence_asset_ids: tuple[UUID, ...],
        source_type: ProductSourceType,
        level_1_candidate: Level1Category,
        level_2_candidate: Level2Category,
    ) -> AlbumProductGroupConfirmation:
        if expected_current_snapshot_version < 1:
            raise ValueError("invalid_snapshot_version")
        if not 2 <= len(evidence_asset_ids) <= 4 or len(set(evidence_asset_ids)) != len(
            evidence_asset_ids
        ):
            raise ValueError("invalid_evidence_group")
        async with UnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            membership = await session.scalar(
                select(OpcMembership).where(
                    OpcMembership.opc_id == opc_id,
                    OpcMembership.user_id == user_id,
                )
            )
            if membership is None or membership.role != "owner":
                raise OpcOwnerRequired("opc_owner_required")
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {
                    "lock_key": (
                        f"album-product-next-snapshot:{opc_id}:{album_product_id}"
                    )
                },
            )
            latest = await session.scalar(
                select(AlbumProductSnapshot)
                .where(
                    AlbumProductSnapshot.opc_id == opc_id,
                    AlbumProductSnapshot.album_product_id == album_product_id,
                )
                .order_by(AlbumProductSnapshot.snapshot_version.desc())
                .limit(1)
                .with_for_update()
            )
            binding = await session.scalar(
                select(ProductCodeBindingRecord)
                .where(
                    ProductCodeBindingRecord.opc_id == opc_id,
                    ProductCodeBindingRecord.album_product_id == album_product_id,
                    ProductCodeBindingRecord.binding_source == "operator_provisioned",
                )
                .with_for_update()
            )
            if latest is None or binding is None:
                raise ExistingProductNotFound("existing_product_not_found")
            try:
                group = await self._validated_group(
                    session,
                    opc_id=opc_id,
                    evidence_asset_ids=evidence_asset_ids,
                )
            except EvidenceGroupNotFound:
                raise ExistingProductNotFound("existing_product_not_found") from None
            existing = await session.scalar(
                select(AlbumProductSnapshot).where(
                    AlbumProductSnapshot.source_event_id == confirmation_request_id
                )
            )
            if existing is not None:
                return await self._replay_existing_product(
                    session,
                    existing=existing,
                    opc_id=opc_id,
                    album_product_id=album_product_id,
                    expected_snapshot_version=expected_current_snapshot_version + 1,
                    evidence_asset_ids=evidence_asset_ids,
                    group=group,
                    source_type=source_type,
                    level_1_candidate=level_1_candidate,
                    level_2_candidate=level_2_candidate,
                    confirmed_by=f"user:{user_id}",
                )
            if await session.get(OutboxEvent, confirmation_request_id) is not None:
                raise ExistingProductConfirmationConflict(
                    "existing_product_confirmation_conflict"
                )
            if latest.snapshot_version != expected_current_snapshot_version:
                raise SnapshotVersionConflict("snapshot_version_conflict")

            now = self._clock()
            next_version = latest.snapshot_version + 1
            confirmed_by = f"user:{user_id}"
            event = ConfirmedSnapshot(
                schema_version="album-product-categorization/1.0",
                event_id=confirmation_request_id,
                event_type=SNAPSHOT_CONFIRMED_TOPIC,
                occurred_at=now,
                opc_id=opc_id,
                album_product_id=album_product_id,
                snapshot_version=next_version,
                source_import_task_id=group.source_import_task_id,
                source_type=source_type,
                images=group.images,
                human_confirmation=HumanConfirmation(
                    status="confirmed",
                    confirmed_by=confirmed_by,
                    confirmed_at=now,
                    confirmation_scope="grouping_membership",
                ),
                categorization_input=CategorizationSeed(
                    level_1_candidate=level_1_candidate,
                    level_2_candidate=level_2_candidate,
                    candidate_source="operator_seed",
                    confidence=None,
                    assertion_scope="candidate_only",
                ),
                consent=Consent(
                    status="not_required",
                    basis="opc_owned_product_material",
                ),
                allowed_consumers=("categorization", "mall_draft_export"),
                mall_product_id=None,
                bead_id=None,
            )
            snapshot = AlbumProductSnapshot(
                source_event_id=event.event_id,
                opc_id=event.opc_id,
                album_product_id=event.album_product_id,
                snapshot_version=event.snapshot_version,
                source_import_task_id=event.source_import_task_id,
                source_type=event.source_type.value,
                occurred_at=event.occurred_at,
                group_confirmed_by=event.human_confirmation.confirmed_by,
                group_confirmed_at=event.human_confirmation.confirmed_at,
                consent_basis=event.consent.basis,
                evidence_resolution_mode="registry_required",
                allowed_consumers=list(event.allowed_consumers),
                images=[image.model_dump(mode="json") for image in event.images],
                source_payload=event.model_dump(mode="json"),
            )
            session.add(snapshot)
            await session.flush()
            batch = ConfirmedSnapshotBatch(
                schema_version="album-product-categorization/1.0",
                batch_id=event.event_id,
                generated_at=event.occurred_at,
                data_classification=("opc_owned_product_material_no_customer_data"),
                events=(event,),
            )
            session.add(
                OutboxEvent(
                    id=event.event_id,
                    topic=SNAPSHOT_CONFIRMED_TOPIC,
                    aggregate_id=snapshot.id,
                    sequence=next_version,
                    payload=batch.model_dump(mode="json"),
                )
            )
            await session.flush()
            return self._result(event, evidence_asset_ids)

    async def _validated_group(
        self,
        session: AsyncSession,
        *,
        opc_id: str,
        evidence_asset_ids: tuple[UUID, ...],
    ) -> _ValidatedGroup:
        rows = (
            await session.execute(
                select(OpcEvidenceAsset, Asset, StoredBlob, ImportTask)
                .join(Asset, Asset.id == OpcEvidenceAsset.source_asset_id)
                .join(StoredBlob, StoredBlob.id == OpcEvidenceAsset.source_blob_id)
                .join(ImportTask, ImportTask.id == Asset.task_id)
                .where(
                    OpcEvidenceAsset.opc_id == opc_id,
                    OpcEvidenceAsset.id.in_(evidence_asset_ids),
                )
                .with_for_update()
            )
        ).all()
        by_id = {
            evidence.id: (evidence, asset, blob, task)
            for evidence, asset, blob, task in rows
        }
        if set(by_id) != set(evidence_asset_ids):
            raise EvidenceGroupNotFound("evidence_group_not_found")
        task_ids: set[UUID] = set()
        images: list[ImageEvidence] = []
        for evidence_id in evidence_asset_ids:
            evidence, asset, blob, task = by_id[evidence_id]
            if not self._valid_chain(evidence, asset, blob, task):
                raise EvidenceGroupNotFound("evidence_group_not_found")
            task_ids.add(task.id)
            images.append(
                ImageEvidence(
                    asset_id=asset.id,
                    role="original",
                    sha256=evidence.sha256,
                    byte_size=evidence.byte_size,
                    media_type=evidence.media_type,
                    captured_at=evidence.captured_at,
                    imported_at=evidence.imported_at,
                    storage_ref=evidence.storage_ref,
                    source_asset_id=None,
                    transform=None,
                )
            )
        if len(task_ids) != 1:
            raise EvidenceGroupNotFound("evidence_group_not_found")
        return _ValidatedGroup(
            source_import_task_id=str(next(iter(task_ids))),
            images=tuple(images),
        )

    async def _replay_existing_product(
        self,
        session: AsyncSession,
        *,
        existing: AlbumProductSnapshot,
        opc_id: str,
        album_product_id: str,
        expected_snapshot_version: int,
        evidence_asset_ids: tuple[UUID, ...],
        group: _ValidatedGroup,
        source_type: ProductSourceType,
        level_1_candidate: Level1Category,
        level_2_candidate: Level2Category,
        confirmed_by: str,
    ) -> AlbumProductGroupConfirmation:
        try:
            event = ConfirmedSnapshot.model_validate(existing.source_payload)
        except ValidationError:
            raise ExistingProductConfirmationConflict(
                "existing_product_confirmation_conflict"
            ) from None
        outbox = await session.get(OutboxEvent, event.event_id)
        try:
            batch = (
                ConfirmedSnapshotBatch.model_validate(outbox.payload)
                if outbox is not None
                else None
            )
        except ValidationError:
            batch = None
        if (
            existing.opc_id != opc_id
            or event.opc_id != opc_id
            or event.album_product_id != album_product_id
            or event.snapshot_version != expected_snapshot_version
            or event.source_import_task_id != group.source_import_task_id
            or event.source_type is not source_type
            or event.images != group.images
            or event.human_confirmation.confirmed_by != confirmed_by
            or event.categorization_input.level_1_candidate is not level_1_candidate
            or event.categorization_input.level_2_candidate is not level_2_candidate
            or outbox is None
            or outbox.topic != SNAPSHOT_CONFIRMED_TOPIC
            or outbox.aggregate_id != existing.id
            or outbox.sequence != expected_snapshot_version
            or batch is None
            or batch.batch_id != event.event_id
            or batch.events != (event,)
        ):
            raise ExistingProductConfirmationConflict(
                "existing_product_confirmation_conflict"
            )
        return self._result(event, evidence_asset_ids)

    @staticmethod
    def _valid_chain(
        evidence: OpcEvidenceAsset,
        asset: Asset,
        blob: StoredBlob,
        task: ImportTask,
    ) -> bool:
        return (
            evidence.storage_ref == f"evidence/{evidence.id}/original"
            and evidence.source_asset_id == asset.id
            and evidence.source_blob_id == asset.blob_id == blob.id
            and evidence.created_by == asset.user_id == blob.user_id == task.user_id
            and evidence.source_object_key == blob.object_key
            and asset.status is AssetStatus.UPLOADED
            and blob.is_ready
            and evidence.sha256 == asset.sha256 == blob.sha256
            and evidence.byte_size == asset.byte_size == blob.byte_size
            and evidence.media_type == asset.media_type
            and evidence.captured_at == asset.captured_at
            and evidence.imported_at == asset.created_at
        )

    async def _replay(
        self,
        session: AsyncSession,
        *,
        existing: AlbumProductSnapshot,
        opc_id: str,
        evidence_asset_ids: tuple[UUID, ...],
        group: _ValidatedGroup,
        source_type: ProductSourceType,
        level_1_candidate: Level1Category,
        level_2_candidate: Level2Category,
        confirmed_by: str,
        album_product_id: str,
    ) -> AlbumProductGroupConfirmation:
        try:
            event = ConfirmedSnapshot.model_validate(existing.source_payload)
        except ValidationError:
            raise EvidenceGroupConflict("group_confirmation_conflict") from None
        outbox = await session.get(OutboxEvent, event.event_id)
        try:
            batch = (
                ConfirmedSnapshotBatch.model_validate(outbox.payload)
                if outbox is not None
                else None
            )
        except ValidationError:
            raise EvidenceGroupConflict("group_confirmation_conflict") from None
        if (
            existing.opc_id != opc_id
            or event.opc_id != opc_id
            or event.album_product_id != album_product_id
            or event.snapshot_version != 1
            or event.source_import_task_id != group.source_import_task_id
            or event.source_type is not source_type
            or event.images != group.images
            or event.human_confirmation.confirmed_by != confirmed_by
            or event.categorization_input.level_1_candidate is not level_1_candidate
            or event.categorization_input.level_2_candidate is not level_2_candidate
            or outbox is None
            or outbox.topic != SNAPSHOT_CONFIRMED_TOPIC
            or outbox.aggregate_id != existing.id
            or outbox.sequence != 1
            or batch is None
            or batch.batch_id != event.event_id
            or batch.generated_at != event.occurred_at
            or batch.data_classification
            != "opc_owned_product_material_no_customer_data"
            or batch.events != (event,)
        ):
            raise EvidenceGroupConflict("group_confirmation_conflict")
        return self._result(event, evidence_asset_ids)

    @staticmethod
    def _result(
        event: ConfirmedSnapshot,
        evidence_asset_ids: tuple[UUID, ...],
    ) -> AlbumProductGroupConfirmation:
        return AlbumProductGroupConfirmation(
            confirmation_event_id=event.event_id,
            album_product_id=event.album_product_id,
            snapshot_version=event.snapshot_version,
            opc_id=event.opc_id,
            evidence_asset_ids=evidence_asset_ids,
            source_type=event.source_type,
            confirmed_by=event.human_confirmation.confirmed_by,
            confirmed_at=event.human_confirmation.confirmed_at,
        )
