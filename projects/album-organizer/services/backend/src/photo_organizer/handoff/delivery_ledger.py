from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.handoff.product_image_set import (
    ClassificationCandidateSource,
    ConfirmedClassificationSource,
    HandoffAdapterError,
    OpcExternalTenantBinding,
    PreparedImageHandoff,
    ProductCodeBinding,
    ProductImageSetReadyV1,
    ProductImageSetSource,
    SourceImage,
    build_product_image_set_ready,
    canonical_body_sha256,
    product_image_set_event_id,
    replay_disposition,
)
from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.handoff_authority import (
    OpcExternalTenantBindingRecord,
    PreparedImageHandoffRecord,
    ProductCodeBindingRecord,
)
from photo_organizer.models.product_image_set_delivery import (
    ProductImageSetDelivery,
    ProductImageSetDeliveryImage,
)
from photo_organizer.repositories.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class DeliveryLedgerReceipt:
    disposition: Literal["created", "replayed"]
    event_id: UUID
    canonical_sha256: str
    payload: dict[str, object]


@dataclass(frozen=True)
class PublicationReceiptState:
    status: Literal["delivered", "retryable_failure", "permanent_failure"]
    retry_after_seconds: int | None
    error_code: str | None

    @classmethod
    def from_http_result(
        cls, status_code: int | None, *, attempt: int
    ) -> PublicationReceiptState:
        if attempt < 1:
            raise ValueError("publication_attempt_must_be_positive")
        if status_code in {200, 202}:
            return cls("delivered", None, None)
        if status_code is None or status_code in {408, 425, 429} or status_code >= 500:
            delays = (30, 120, 600, 1800, 7200)
            error_code = (
                "network_error" if status_code is None else f"http_{status_code}"
            )
            return cls(
                "retryable_failure",
                delays[min(attempt - 1, len(delays) - 1)],
                error_code,
            )
        return cls("permanent_failure", None, f"http_{status_code}")


class ProductImageSetDeliveryLedger:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def record_publication_receipt(
        self,
        *,
        event_id: UUID,
        canonical_sha256: str,
        state: PublicationReceiptState,
    ) -> None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            delivery = await unit_of_work.session.scalar(
                select(ProductImageSetDelivery)
                .where(
                    ProductImageSetDelivery.event_id == event_id,
                    ProductImageSetDelivery.canonical_sha256 == canonical_sha256,
                )
                .with_for_update()
            )
            if delivery is None:
                raise HandoffAdapterError("publication_delivery_not_found")
            delivery.publication_attempts += 1
            delivery.publication_status = state.status
            if state.status == "delivered":
                delivery.publication_next_attempt_at = None
                delivery.publication_last_error_code = None
                delivery.publication_delivered_at = datetime.now(UTC)
            elif state.status == "retryable_failure":
                delivery.publication_next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=state.retry_after_seconds or 0
                )
                delivery.publication_last_error_code = state.error_code
                delivery.publication_delivered_at = None
            else:
                delivery.publication_next_attempt_at = None
                delivery.publication_last_error_code = state.error_code
                delivery.publication_delivered_at = None

    async def queue_ready_delivery(
        self,
        *,
        source_classification_id: UUID,
    ) -> DeliveryLedgerReceipt:
        """Discover the authoritative handoff records and queue one delivery.

        This is deliberately fail-closed: absence of an operator-provisioned
        tenant/product binding or of a complete prepared-image set is passed to
        ``record_from_authority`` as an empty/missing authority set, where the
        existing immutable-event checks reject it.
        """
        async with self._session_factory() as session:
            classification = await session.scalar(
                select(ProductClassification).where(
                    ProductClassification.id == source_classification_id
                )
            )
            if classification is None:
                raise HandoffAdapterError("handoff_authority_not_found")
            product = await session.scalar(
                select(ProductCodeBindingRecord).where(
                    ProductCodeBindingRecord.opc_id == classification.opc_id,
                    ProductCodeBindingRecord.album_product_id
                    == classification.album_product_id,
                )
            )
            if product is None:
                raise HandoffAdapterError("handoff_authority_not_found")
            tenant = await session.scalar(
                select(OpcExternalTenantBindingRecord).where(
                    OpcExternalTenantBindingRecord.opc_id == classification.opc_id
                )
            )
            if tenant is None:
                raise HandoffAdapterError("handoff_authority_not_found")
            prepared_image_binding_ids = tuple(
                (
                    await session.scalars(
                        select(PreparedImageHandoffRecord.id)
                        .where(
                            PreparedImageHandoffRecord.source_classification_id
                            == classification.id,
                            PreparedImageHandoffRecord.product_binding_id == product.id,
                        )
                        .order_by(PreparedImageHandoffRecord.asset_id)
                    )
                ).all()
            )
        return await self.record_from_authority(
            source_classification_id=source_classification_id,
            tenant_binding_id=tenant.id,
            product_binding_id=product.id,
            prepared_image_binding_ids=prepared_image_binding_ids,
        )

    async def enqueue_ready_for_classification(
        self,
        *,
        source_classification_id: UUID,
    ) -> DeliveryLedgerReceipt:
        """Compatibility name for the bounded delivery-queue worker."""
        return await self.queue_ready_delivery(
            source_classification_id=source_classification_id
        )

    async def record_from_authority(
        self,
        *,
        source_classification_id: UUID,
        tenant_binding_id: UUID,
        product_binding_id: UUID,
        prepared_image_binding_ids: tuple[UUID, ...],
    ) -> DeliveryLedgerReceipt:
        if not 1 <= len(prepared_image_binding_ids) <= 3000 or len(
            set(prepared_image_binding_ids)
        ) != len(prepared_image_binding_ids):
            raise HandoffAdapterError("prepared_image_set_mismatch")
        event_id = product_image_set_event_id(source_classification_id)
        async with UnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"product-image-set-delivery:{event_id}"},
            )
            prepared_ids = sorted(str(value) for value in prepared_image_binding_ids)
            existing = await session.scalar(
                select(ProductImageSetDelivery)
                .where(ProductImageSetDelivery.event_id == event_id)
                .with_for_update()
            )
            if existing is not None:
                linked_ids = sorted(
                    str(value)
                    for value in (
                        await session.scalars(
                            select(
                                ProductImageSetDeliveryImage.prepared_image_id
                            ).where(
                                ProductImageSetDeliveryImage.delivery_id == existing.id
                            )
                        )
                    ).all()
                )
                if (
                    existing.source_classification_id != source_classification_id
                    or existing.tenant_binding_id != tenant_binding_id
                    or existing.product_binding_id != product_binding_id
                    or existing.prepared_image_binding_ids != prepared_ids
                    or linked_ids != prepared_ids
                ):
                    raise HandoffAdapterError("event_identity_conflict")

            event, source_snapshot_id = await self._build_from_authority(
                session,
                source_classification_id=source_classification_id,
                tenant_binding_id=tenant_binding_id,
                product_binding_id=product_binding_id,
                prepared_image_binding_ids=prepared_image_binding_ids,
            )
            if event.event_id != event_id:
                raise HandoffAdapterError("event_identity_conflict")
            payload = event.model_dump(mode="json")
            digest = canonical_body_sha256(event)
            if existing is not None:
                disposition = replay_disposition(existing.canonical_sha256, digest)
                if (
                    existing.payload != payload
                    or existing.source_classification_id != source_classification_id
                    or existing.source_snapshot_id != source_snapshot_id
                    or existing.tenant_binding_id != tenant_binding_id
                    or existing.product_binding_id != product_binding_id
                ):
                    raise HandoffAdapterError("event_identity_conflict")
                return DeliveryLedgerReceipt(
                    disposition=disposition,
                    event_id=existing.event_id,
                    canonical_sha256=existing.canonical_sha256,
                    payload=existing.payload,
                )
            delivery = ProductImageSetDelivery(
                event_id=event.event_id,
                source_classification_id=source_classification_id,
                source_snapshot_id=source_snapshot_id,
                tenant_binding_id=tenant_binding_id,
                product_binding_id=product_binding_id,
                prepared_image_binding_ids=prepared_ids,
                canonical_sha256=digest,
                payload=payload,
            )
            session.add(delivery)
            await session.flush()
            session.add_all(
                ProductImageSetDeliveryImage(
                    delivery_id=delivery.id,
                    prepared_image_id=prepared_image_id,
                )
                for prepared_image_id in prepared_image_binding_ids
            )
            await session.flush()
            return DeliveryLedgerReceipt(
                disposition="created",
                event_id=event.event_id,
                canonical_sha256=digest,
                payload=payload,
            )

    @staticmethod
    async def _build_from_authority(
        session: AsyncSession,
        *,
        source_classification_id: UUID,
        tenant_binding_id: UUID,
        product_binding_id: UUID,
        prepared_image_binding_ids: tuple[UUID, ...],
    ) -> tuple[ProductImageSetReadyV1, UUID]:
        classification = await session.scalar(
            select(ProductClassification)
            .where(ProductClassification.id == source_classification_id)
            .with_for_update()
        )
        tenant = await session.scalar(
            select(OpcExternalTenantBindingRecord)
            .where(OpcExternalTenantBindingRecord.id == tenant_binding_id)
            .with_for_update()
        )
        product = await session.scalar(
            select(ProductCodeBindingRecord)
            .where(ProductCodeBindingRecord.id == product_binding_id)
            .with_for_update()
        )
        if classification is None or tenant is None or product is None:
            raise HandoffAdapterError("handoff_authority_not_found")
        snapshot = await session.scalar(
            select(AlbumProductSnapshot)
            .where(
                AlbumProductSnapshot.source_event_id == classification.input_event_id
            )
            .with_for_update()
        )
        prepared = (
            await session.scalars(
                select(PreparedImageHandoffRecord)
                .where(PreparedImageHandoffRecord.id.in_(prepared_image_binding_ids))
                .with_for_update()
            )
        ).all()
        if snapshot is None or len(prepared) != len(prepared_image_binding_ids):
            raise HandoffAdapterError("handoff_authority_not_found")
        if any(
            image.product_binding_id != product.id
            or image.source_classification_id != classification.id
            for image in prepared
        ):
            raise HandoffAdapterError("prepared_image_authority_mismatch")
        try:
            source = ProductImageSetSource(
                opc_id=snapshot.opc_id,
                album_product_id=snapshot.album_product_id,
                snapshot_version=snapshot.snapshot_version,
                source_import_task_id=snapshot.source_import_task_id,
                images=tuple(
                    SourceImage.model_validate(
                        {
                            "asset_id": image["asset_id"],
                            "sha256": image["sha256"],
                            "mime_type": image["media_type"],
                            "kind": image["role"],
                        }
                    )
                    for image in snapshot.images
                ),
            )
            confirmed = ConfirmedClassificationSource.model_validate(
                {
                    "classification_id": classification.id,
                    "opc_id": classification.opc_id,
                    "album_product_id": classification.album_product_id,
                    "input_snapshot_version": classification.input_snapshot_version,
                    "status": classification.status,
                    "confirmed_level_1": classification.confirmed_level_1,
                    "confirmed_level_2": classification.confirmed_level_2,
                    "confirmed_at": classification.confirmed_at,
                    "candidates": tuple(
                        ClassificationCandidateSource.model_validate(
                            {
                                "level_1": candidate["level_1"],
                                "level_2": candidate["level_2"],
                                "confidence": candidate["confidence"],
                            }
                        )
                        for candidate in classification.candidates
                    ),
                }
            )
            tenant_binding = OpcExternalTenantBinding.model_validate(
                {
                    "binding_id": tenant.id,
                    "binding_source": tenant.binding_source,
                    "opc_id": tenant.opc_id,
                    "tenant_id": tenant.tenant_id,
                }
            )
            product_binding = ProductCodeBinding.model_validate(
                {
                    "binding_id": product.id,
                    "binding_source": product.binding_source,
                    "opc_id": product.opc_id,
                    "album_product_id": product.album_product_id,
                    "product_code": product.product_code,
                }
            )
            prepared_inputs = tuple(
                PreparedImageHandoff.model_validate(
                    {
                        "asset_id": image.asset_id,
                        "object_key": image.object_key,
                        "role": image.role,
                        "quality_confidence": image.quality_confidence,
                        "storage_status": image.storage_status,
                    }
                )
                for image in prepared
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise HandoffAdapterError("handoff_authority_invalid") from None
        return (
            build_product_image_set_ready(
                source,
                confirmed,
                tenant_binding,
                product_binding,
                prepared_inputs,
            ),
            snapshot.id,
        )
