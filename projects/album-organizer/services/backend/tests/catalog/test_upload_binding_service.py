from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.catalog.upload_bindings import (
    UPLOAD_CANDIDATE_BOUND_TOPIC,
    UploadBindingConflict,
    UploadBindingInvalidCandidate,
    UploadBindingNotFound,
    UploadBindingService,
)
from photo_organizer.domain.enums import TaskStatus
from photo_organizer.models.asset import Asset
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.task import ImportTask
from photo_organizer.models.upload_binding import ControlledUploadBinding
from photo_organizer.models.user import User
from photo_organizer.schemas.controlled_upload_candidate import (
    ControlledUploadCandidate,
    controlled_upload_candidate_id,
)
from photo_organizer.schemas.upload_bindings import UploadCandidateBoundEvent

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)
OPC_PREFIX = "opc-upload-binding-test-"
TASK_NAME = "Authenticated upload binding fixture"


@pytest_asyncio.fixture(autouse=True)
async def clear_upload_binding_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
):
    async def clear() -> None:
        async with auth_session_factory() as session:
            await session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.topic == UPLOAD_CANDIDATE_BOUND_TOPIC
                )
            )
            await session.execute(delete(ControlledUploadBinding))
            await session.execute(
                delete(ImportTask).where(ImportTask.name == TASK_NAME)
            )
            await session.execute(
                delete(OpcMembership).where(OpcMembership.opc_id.like(f"{OPC_PREFIX}%"))
            )
            await session.execute(
                delete(OpcWorkspace).where(OpcWorkspace.id.like(f"{OPC_PREFIX}%"))
            )
            await session.commit()

    await clear()
    yield
    await clear()


@dataclass(frozen=True)
class SeededBinding:
    opc_id: str
    user_id: UUID
    task_ids: tuple[UUID, ...]


def _candidate(opc_id: str, *, marker: str = "a") -> ControlledUploadCandidate:
    first_id = uuid4()
    second_id = uuid4()
    body: dict[str, object] = {
        "schema_version": "opc-controlled-upload-candidate/1.0",
        "status": "ready_for_authenticated_task_binding",
        "source_readiness_id": f"opc-real-sku-pilot-readiness:{marker * 64}",
        "opc_id": opc_id,
        "opc_authority_state": "requires_authenticated_revalidation",
        "album_product_id": f"album-product-{marker * 8}",
        "source_type": "first_sale_bead",
        "target_chain": "sellable_product_material",
        "data_classification": "opc_owned_product_material_no_customer_data",
        "material_owner": "opc",
        "authorization_basis": "opc_self_owned_product_material",
        "authorized_by": "user:33333333-3333-4333-8333-333333333333",
        "authorized_at": NOW.isoformat().replace("+00:00", "Z"),
        "privacy_review_confirmed": True,
        "customer_data_present": False,
        "items": (
            {
                "local_asset_id": str(first_id),
                "role": "primary",
                "position": 1,
                "filename": f"{first_id}.jpg",
                "media_type": "image/jpeg",
                "byte_size": 123,
                "sha256": "1" * 64,
                "captured_at": NOW.isoformat().replace("+00:00", "Z"),
                "human_privacy_reviewed": True,
                "privacy_metadata_status": "no_sensitive_metadata_detected",
                "retry": False,
            },
            {
                "local_asset_id": str(second_id),
                "role": "detail",
                "position": 2,
                "filename": f"{second_id}.png",
                "media_type": "image/png",
                "byte_size": 456,
                "sha256": "2" * 64,
                "captured_at": NOW.isoformat().replace("+00:00", "Z"),
                "human_privacy_reviewed": True,
                "privacy_metadata_status": "no_sensitive_metadata_detected",
                "retry": False,
            },
        ),
        "claims_not_evaluated": (
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        ),
        "target_task_id": None,
        "task_binding_state": "not_bound",
        "upload_state": "not_started",
        "next_action": "bind_to_verified_opc_upload_task",
        "mall_product_id": None,
        "bead_id": None,
        "publication_state": "not_created",
        "auto_publish": False,
        "shared_market_opt_in": False,
    }
    body["candidate_id"] = controlled_upload_candidate_id(body)
    return ControlledUploadCandidate.model_validate(body)


async def _seed_binding(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    statuses: tuple[TaskStatus, ...] = (TaskStatus.WAITING_UPLOAD,),
    membership: bool = True,
    opc_id: str | None = None,
) -> SeededBinding:
    suffix = uuid4().hex
    workspace_id = opc_id or f"{OPC_PREFIX}{suffix}"
    user = User(phone_e164=f"+86135{str(uuid4().int)[-8:]}")
    workspace = OpcWorkspace(id=workspace_id, display_name="Upload Binding OPC")
    tasks = [
        ImportTask(
            user_id=user.id,
            name=TASK_NAME,
            status=status,
        )
        for status in statuses
    ]
    async with session_factory() as session:
        session.add_all([user, workspace])
        await session.flush()
        for task in tasks:
            task.user_id = user.id
        if membership:
            session.add(
                OpcMembership(opc_id=workspace_id, user_id=user.id, role="owner")
            )
        session.add_all(tasks)
        await session.commit()
    return SeededBinding(
        opc_id=workspace_id,
        user_id=user.id,
        task_ids=tuple(task.id for task in tasks),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (TaskStatus.WAITING_UPLOAD, TaskStatus.UPLOADING))
async def test_binds_open_task_and_emits_versioned_safe_event(
    auth_session_factory: async_sessionmaker[AsyncSession],
    status: TaskStatus,
) -> None:
    seeded = await _seed_binding(auth_session_factory, statuses=(status,))
    candidate = _candidate(seeded.opc_id)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)

    result = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )

    assert result.schema_version == "opc-controlled-upload-binding/1.0"
    assert result.candidate_id == candidate.candidate_id
    assert result.source_readiness_id == candidate.source_readiness_id
    assert result.opc_id == seeded.opc_id
    assert result.task_id == seeded.task_ids[0]
    assert result.bound_by == f"user:{seeded.user_id}"
    assert result.bound_at == NOW
    assert result.opc_authority_state == (
        "verified_by_authenticated_membership_and_task_binding"
    )
    assert result.task_binding_state == "bound"
    assert result.upload_state == "ready_for_queue_handoff"
    assert result.next_action == "enqueue_candidate_items"
    assert result.mall_product_id is None and result.bead_id is None
    assert result.publication_state == "not_created"
    assert result.auto_publish is False
    assert result.shared_market_opt_in is False

    async with auth_session_factory() as session:
        task = await session.get(ImportTask, seeded.task_ids[0])
        binding = await session.get(ControlledUploadBinding, result.binding_id)
        outbox = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == UPLOAD_CANDIDATE_BOUND_TOPIC,
                OutboxEvent.aggregate_id == result.binding_id,
            )
        )
        asset_count = await session.scalar(
            select(func.count(Asset.id)).where(Asset.task_id == seeded.task_ids[0])
        )
        blob_count = await session.scalar(
            select(func.count(StoredBlob.id)).where(
                StoredBlob.user_id == seeded.user_id
            )
        )
    assert task is not None and task.opc_id == seeded.opc_id
    assert binding is not None
    assert binding.candidate_payload == candidate.model_dump(mode="json")
    assert outbox is not None
    event = UploadCandidateBoundEvent.model_validate(outbox.payload)
    assert event.binding_id == binding.id == result.binding_id
    assert event.event_type == "upload.candidate.bound"
    assert event.occurred_at == NOW
    assert event.opc_id == seeded.opc_id
    assert event.task_id == seeded.task_ids[0]
    assert event.bound_by == f"user:{seeded.user_id}"
    assert event.mall_product_id is None and event.bead_id is None
    assert asset_count == 0
    assert blob_count == 0
    rendered = outbox.payload.__str__().casefold()
    for forbidden in (
        "path",
        "url",
        "token",
        "object_key",
        "image_bytes",
        "customer",
        "phone",
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_exact_replay_returns_one_binding_and_one_event(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_binding(auth_session_factory)
    candidate = _candidate(seeded.opc_id)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)

    first = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )
    replay = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )

    assert replay == first
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(ControlledUploadBinding.id))) == 1
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic == UPLOAD_CANDIDATE_BOUND_TOPIC
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_exact_replay_backfills_a_legacy_binding_authority_snapshot(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_binding(auth_session_factory)
    candidate = _candidate(seeded.opc_id)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)
    first = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )
    async with auth_session_factory() as session:
        binding = await session.get(ControlledUploadBinding, first.binding_id)
        assert binding is not None
        binding.candidate_payload = None
        await session.commit()

    replay = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )

    assert replay == first
    async with auth_session_factory() as session:
        binding = await session.get(ControlledUploadBinding, first.binding_id)
        assert binding is not None
        assert binding.candidate_payload == candidate.model_dump(mode="json")


@pytest.mark.asyncio
async def test_replay_rejects_a_damaged_candidate_authority_snapshot(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_binding(auth_session_factory)
    candidate = _candidate(seeded.opc_id)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)
    first = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )
    async with auth_session_factory() as session:
        binding = await session.get(ControlledUploadBinding, first.binding_id)
        assert binding is not None and binding.candidate_payload is not None
        binding.candidate_payload = {
            **binding.candidate_payload,
            "album_product_id": "album-product-damaged",
        }
        await session.commit()

    with pytest.raises(UploadBindingConflict, match="^candidate_already_bound$"):
        await service.bind(
            opc_id=seeded.opc_id,
            user_id=seeded.user_id,
            task_id=seeded.task_ids[0],
            candidate=candidate,
        )


@pytest.mark.asyncio
async def test_exact_replay_survives_task_reaching_a_terminal_state(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_binding(auth_session_factory)
    candidate = _candidate(seeded.opc_id)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)
    first = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )
    async with auth_session_factory() as session:
        task = await session.get(ImportTask, seeded.task_ids[0])
        assert task is not None
        task.status = TaskStatus.COMPLETED
        await session.commit()

    replay = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )

    assert replay == first
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(ControlledUploadBinding.id))) == 1
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic == UPLOAD_CANDIDATE_BOUND_TOPIC
                )
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("nonserializable_item", "semantic_tamper"))
async def test_forged_candidate_is_rejected_with_one_stable_error(
    auth_session_factory: async_sessionmaker[AsyncSession],
    mutation: str,
) -> None:
    seeded = await _seed_binding(auth_session_factory)
    candidate = _candidate(seeded.opc_id)
    if mutation == "nonserializable_item":
        forged_item = candidate.items[0].model_copy(update={"sha256": object()})
        candidate = candidate.model_copy(
            update={"items": (forged_item, *candidate.items[1:])}
        )
    else:
        candidate = candidate.model_copy(update={"source_type": "legacy_material"})

    with pytest.raises(UploadBindingInvalidCandidate, match="^invalid_candidate$"):
        await UploadBindingService(auth_session_factory, clock=lambda: NOW).bind(
            opc_id=seeded.opc_id,
            user_id=seeded.user_id,
            task_id=seeded.task_ids[0],
            candidate=candidate,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ("missing", "tampered"))
async def test_replay_fails_closed_when_the_outbox_authority_is_damaged(
    auth_session_factory: async_sessionmaker[AsyncSession],
    damage: str,
) -> None:
    seeded = await _seed_binding(auth_session_factory)
    candidate = _candidate(seeded.opc_id)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)
    result = await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )
    async with auth_session_factory() as session:
        outbox = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.topic == UPLOAD_CANDIDATE_BOUND_TOPIC,
                OutboxEvent.aggregate_id == result.binding_id,
            )
        )
        assert outbox is not None
        if damage == "missing":
            await session.delete(outbox)
        else:
            outbox.payload = {**outbox.payload, "task_id": str(uuid4())}
        await session.commit()

    with pytest.raises(UploadBindingConflict, match="^candidate_already_bound$"):
        await service.bind(
            opc_id=seeded.opc_id,
            user_id=seeded.user_id,
            task_id=seeded.task_ids[0],
            candidate=candidate,
        )

    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(ControlledUploadBinding.id))) == 1
        assert await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.topic == UPLOAD_CANDIDATE_BOUND_TOPIC
            )
        ) == (0 if damage == "missing" else 1)


@pytest.mark.asyncio
async def test_missing_membership_and_cross_user_task_are_tenant_neutral(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    without_membership = await _seed_binding(auth_session_factory, membership=False)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)
    with pytest.raises(UploadBindingNotFound, match="^opc_not_found$"):
        await service.bind(
            opc_id=without_membership.opc_id,
            user_id=without_membership.user_id,
            task_id=without_membership.task_ids[0],
            candidate=_candidate(without_membership.opc_id),
        )

    seeded = await _seed_binding(auth_session_factory)
    other = User(phone_e164=f"+86136{str(uuid4().int)[-8:]}")
    async with auth_session_factory() as session:
        session.add(other)
        await session.flush()
        session.add(
            OpcMembership(opc_id=seeded.opc_id, user_id=other.id, role="reviewer")
        )
        await session.commit()
    with pytest.raises(UploadBindingNotFound, match="^task_not_found$"):
        await service.bind(
            opc_id=seeded.opc_id,
            user_id=other.id,
            task_id=seeded.task_ids[0],
            candidate=_candidate(seeded.opc_id, marker="b"),
        )


@pytest.mark.asyncio
async def test_candidate_opc_mismatch_and_cross_opc_task_are_rejected(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_binding(auth_session_factory)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)
    with pytest.raises(UploadBindingConflict, match="^candidate_opc_mismatch$"):
        await service.bind(
            opc_id=seeded.opc_id,
            user_id=seeded.user_id,
            task_id=seeded.task_ids[0],
            candidate=_candidate(f"{OPC_PREFIX}{uuid4().hex}"),
        )

    await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=_candidate(seeded.opc_id, marker="c"),
    )
    other_opc = f"{OPC_PREFIX}{uuid4().hex}"
    async with auth_session_factory() as session:
        session.add(OpcWorkspace(id=other_opc, display_name="Other OPC"))
        await session.flush()
        session.add(
            OpcMembership(opc_id=other_opc, user_id=seeded.user_id, role="owner")
        )
        await session.commit()
    with pytest.raises(UploadBindingNotFound, match="^task_not_found$"):
        await service.bind(
            opc_id=other_opc,
            user_id=seeded.user_id,
            task_id=seeded.task_ids[0],
            candidate=_candidate(other_opc, marker="d"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    (
        TaskStatus.PAUSED,
        TaskStatus.AI_PROCESSING,
        TaskStatus.WAITING_REVIEW,
        TaskStatus.COMPLETED,
        TaskStatus.PARTIAL_FAILURE,
        TaskStatus.CANCELLED,
    ),
)
async def test_closed_task_states_are_not_bindable(
    auth_session_factory: async_sessionmaker[AsyncSession],
    status: TaskStatus,
) -> None:
    seeded = await _seed_binding(auth_session_factory, statuses=(status,))
    with pytest.raises(UploadBindingConflict, match="^task_not_open$"):
        await UploadBindingService(auth_session_factory, clock=lambda: NOW).bind(
            opc_id=seeded.opc_id,
            user_id=seeded.user_id,
            task_id=seeded.task_ids[0],
            candidate=_candidate(seeded.opc_id),
        )


@pytest.mark.asyncio
async def test_candidate_cannot_move_to_a_different_task(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_binding(
        auth_session_factory,
        statuses=(TaskStatus.WAITING_UPLOAD, TaskStatus.UPLOADING),
    )
    candidate = _candidate(seeded.opc_id)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)
    await service.bind(
        opc_id=seeded.opc_id,
        user_id=seeded.user_id,
        task_id=seeded.task_ids[0],
        candidate=candidate,
    )

    with pytest.raises(UploadBindingConflict, match="^candidate_already_bound$"):
        await service.bind(
            opc_id=seeded.opc_id,
            user_id=seeded.user_id,
            task_id=seeded.task_ids[1],
            candidate=candidate,
        )


@pytest.mark.asyncio
async def test_concurrent_candidate_binding_has_one_winner_and_stable_loser(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_binding(auth_session_factory)
    other_user = User(phone_e164=f"+86134{str(uuid4().int)[-8:]}")
    other_task = ImportTask(
        user_id=other_user.id,
        name=TASK_NAME,
        status=TaskStatus.WAITING_UPLOAD,
    )
    async with auth_session_factory() as session:
        session.add(other_user)
        await session.flush()
        other_task.user_id = other_user.id
        session.add_all(
            [
                OpcMembership(
                    opc_id=seeded.opc_id,
                    user_id=other_user.id,
                    role="reviewer",
                ),
                other_task,
            ]
        )
        await session.commit()
    candidate = _candidate(seeded.opc_id)
    service = UploadBindingService(auth_session_factory, clock=lambda: NOW)
    async with auth_session_factory() as session:
        await session.execute(
            text(
                "CREATE OR REPLACE FUNCTION test_pause_upload_binding_insert() "
                "RETURNS trigger AS $$ BEGIN PERFORM pg_sleep(0.15); "
                "RETURN NEW; END $$ LANGUAGE plpgsql"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER test_pause_upload_binding_insert_trigger "
                "BEFORE INSERT ON controlled_upload_bindings FOR EACH ROW "
                "EXECUTE FUNCTION test_pause_upload_binding_insert()"
            )
        )
        await session.commit()

    try:
        results = await asyncio.gather(
            *(
                service.bind(
                    opc_id=seeded.opc_id,
                    user_id=user_id,
                    task_id=task_id,
                    candidate=candidate,
                )
                for user_id, task_id in (
                    (seeded.user_id, seeded.task_ids[0]),
                    (other_user.id, other_task.id),
                )
            ),
            return_exceptions=True,
        )
    finally:
        async with auth_session_factory() as session:
            await session.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "test_pause_upload_binding_insert_trigger "
                    "ON controlled_upload_bindings"
                )
            )
            await session.execute(
                text("DROP FUNCTION IF EXISTS test_pause_upload_binding_insert()")
            )
            await session.commit()

    successes = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], UploadBindingConflict)
    assert str(failures[0]) == "candidate_already_bound"
    async with auth_session_factory() as session:
        assert await session.scalar(select(func.count(ControlledUploadBinding.id))) == 1
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.topic == UPLOAD_CANDIDATE_BOUND_TOPIC
                )
            )
            == 1
        )
