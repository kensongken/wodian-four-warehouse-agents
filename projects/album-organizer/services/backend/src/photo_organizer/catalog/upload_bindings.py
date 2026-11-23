from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.domain.enums import TaskStatus
from photo_organizer.models.opc import OpcMembership
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask
from photo_organizer.models.upload_binding import ControlledUploadBinding
from photo_organizer.repositories.unit_of_work import UnitOfWork
from photo_organizer.schemas.controlled_upload_candidate import (
    ControlledUploadCandidate,
)
from photo_organizer.schemas.upload_bindings import (
    ControlledUploadBindingResponse,
    UploadCandidateBoundEvent,
)

UPLOAD_CANDIDATE_BOUND_TOPIC: Final = "upload.candidate.bound.v1"
_OPEN_TASK_STATUSES: Final = frozenset(
    (TaskStatus.WAITING_UPLOAD, TaskStatus.UPLOADING)
)
_CLAIMS_NOT_EVALUATED: Final = (
    "authenticity",
    "origin",
    "quality",
    "warehouse_acceptance",
)


class UploadBindingError(Exception):
    """Base class for stable authenticated upload-binding failures."""


class UploadBindingNotFound(UploadBindingError):
    """The requested OPC or task is unavailable to the authenticated user."""


class UploadBindingConflict(UploadBindingError):
    """The candidate or task cannot safely enter the requested binding."""


class UploadBindingInvalidCandidate(UploadBindingError):
    """The supplied candidate could not be safely reconstructed."""


class UploadBindingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def bind(
        self,
        *,
        opc_id: str,
        user_id: UUID,
        task_id: UUID,
        candidate: ControlledUploadCandidate,
    ) -> ControlledUploadBindingResponse:
        candidate = self._validated_candidate(candidate)
        if candidate.opc_id != opc_id:
            raise UploadBindingConflict("candidate_opc_mismatch")

        async with UnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"controlled-upload-candidate:{candidate.candidate_id}"},
            )
            membership = await session.scalar(
                select(OpcMembership)
                .where(
                    OpcMembership.opc_id == opc_id,
                    OpcMembership.user_id == user_id,
                )
                .with_for_update()
            )
            if membership is None:
                raise UploadBindingNotFound("opc_not_found")

            existing = await session.scalar(
                select(ControlledUploadBinding)
                .where(ControlledUploadBinding.candidate_id == candidate.candidate_id)
                .with_for_update()
            )
            task = await session.scalar(
                select(ImportTask)
                .where(ImportTask.id == task_id, ImportTask.user_id == user_id)
                .with_for_update()
            )
            if task is None or (task.opc_id is not None and task.opc_id != opc_id):
                raise UploadBindingNotFound("task_not_found")

            if existing is not None:
                return await self._replay(
                    session,
                    existing=existing,
                    opc_id=opc_id,
                    user_id=user_id,
                    task_id=task_id,
                    candidate=candidate,
                )
            if task.status not in _OPEN_TASK_STATUSES:
                raise UploadBindingConflict("task_not_open")

            if task.opc_id is None:
                task.opc_id = opc_id
                await session.flush()

            now = self._utc_now()
            binding = ControlledUploadBinding(
                id=uuid4(),
                candidate_id=candidate.candidate_id,
                source_readiness_id=candidate.source_readiness_id,
                opc_id=opc_id,
                task_id=task.id,
                user_id=user_id,
                album_product_id=candidate.album_product_id,
                source_type=candidate.source_type.value,
                item_count=len(candidate.items),
                candidate_payload=candidate.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            response = self._response(binding)
            event = self._event(response)
            session.add(binding)
            session.add(
                OutboxEvent(
                    topic=UPLOAD_CANDIDATE_BOUND_TOPIC,
                    aggregate_id=binding.id,
                    sequence=1,
                    payload=event.model_dump(mode="json"),
                )
            )
            await session.flush()
            return response

    @staticmethod
    def _validated_candidate(
        candidate: ControlledUploadCandidate,
    ) -> ControlledUploadCandidate:
        try:
            return ControlledUploadCandidate.model_validate(
                candidate.model_dump(mode="json")
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            ValidationError,
            PydanticSerializationError,
        ):
            raise UploadBindingInvalidCandidate("invalid_candidate") from None

    async def _replay(
        self,
        session: AsyncSession,
        *,
        existing: ControlledUploadBinding,
        opc_id: str,
        user_id: UUID,
        task_id: UUID,
        candidate: ControlledUploadCandidate,
    ) -> ControlledUploadBindingResponse:
        if not self._matches(
            existing,
            opc_id=opc_id,
            user_id=user_id,
            task_id=task_id,
            candidate=candidate,
        ):
            raise UploadBindingConflict("candidate_already_bound")
        response = self._response(existing)
        expected_event = self._event(response)
        outbox = await session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.topic == UPLOAD_CANDIDATE_BOUND_TOPIC,
                OutboxEvent.aggregate_id == existing.id,
                OutboxEvent.sequence == 1,
            )
            .with_for_update()
        )
        try:
            actual_event = (
                UploadCandidateBoundEvent.model_validate(outbox.payload)
                if outbox is not None
                else None
            )
        except ValidationError:
            actual_event = None
        if actual_event != expected_event:
            raise UploadBindingConflict("candidate_already_bound")
        if existing.candidate_payload is None:
            existing.candidate_payload = candidate.model_dump(mode="json")
            await session.flush()
        return response

    @staticmethod
    def _matches(
        binding: ControlledUploadBinding,
        *,
        opc_id: str,
        user_id: UUID,
        task_id: UUID,
        candidate: ControlledUploadCandidate,
    ) -> bool:
        return (
            binding.candidate_id == candidate.candidate_id
            and binding.source_readiness_id == candidate.source_readiness_id
            and binding.opc_id == opc_id == candidate.opc_id
            and binding.task_id == task_id
            and binding.user_id == user_id
            and binding.album_product_id == candidate.album_product_id
            and binding.source_type == candidate.source_type.value
            and binding.item_count == len(candidate.items)
            and (
                binding.candidate_payload is None
                or binding.candidate_payload == candidate.model_dump(mode="json")
            )
        )

    @staticmethod
    def _response(
        binding: ControlledUploadBinding,
    ) -> ControlledUploadBindingResponse:
        return ControlledUploadBindingResponse(
            schema_version="opc-controlled-upload-binding/1.0",
            binding_id=binding.id,
            candidate_id=binding.candidate_id,
            source_readiness_id=binding.source_readiness_id,
            opc_id=binding.opc_id,
            task_id=binding.task_id,
            album_product_id=binding.album_product_id,
            source_type="first_sale_bead",
            item_count=binding.item_count,
            bound_by=f"user:{binding.user_id}",
            bound_at=binding.created_at,
            opc_authority_state=(
                "verified_by_authenticated_membership_and_task_binding"
            ),
            task_binding_state="bound",
            upload_state="ready_for_queue_handoff",
            next_action="enqueue_candidate_items",
            claims_not_evaluated=_CLAIMS_NOT_EVALUATED,
            mall_product_id=None,
            bead_id=None,
            publication_state="not_created",
            auto_publish=False,
            shared_market_opt_in=False,
        )

    @staticmethod
    def _event(response: ControlledUploadBindingResponse) -> UploadCandidateBoundEvent:
        return UploadCandidateBoundEvent(
            schema_version="opc-controlled-upload-binding-event/1.0",
            event_type="upload.candidate.bound",
            event_version=1,
            binding_id=response.binding_id,
            candidate_id=response.candidate_id,
            source_readiness_id=response.source_readiness_id,
            opc_id=response.opc_id,
            task_id=response.task_id,
            album_product_id=response.album_product_id,
            source_type=response.source_type,
            item_count=response.item_count,
            bound_by=response.bound_by,
            occurred_at=response.bound_at,
            opc_authority_state=response.opc_authority_state,
            task_binding_state=response.task_binding_state,
            upload_state=response.upload_state,
            next_action=response.next_action,
            claims_not_evaluated=response.claims_not_evaluated,
            mall_product_id=None,
            bead_id=None,
            publication_state="not_created",
            auto_publish=False,
            shared_market_opt_in=False,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("upload binding clock must be timezone-aware")
        return value.astimezone(UTC)
