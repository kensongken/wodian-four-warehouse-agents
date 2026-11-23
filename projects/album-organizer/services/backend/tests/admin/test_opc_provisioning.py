import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.admin.opc_provisioning import (
    OpcProvisioningError,
    OpcProvisioningService,
)
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.user import User

OWNER_ID = UUID("00000000-0000-0000-0000-000000000101")


@pytest_asyncio.fixture(autouse=True)
async def clean_opc_authority(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async def clean() -> None:
        async with auth_session_factory() as session:
            await session.execute(delete(OpcMembership))
            await session.execute(delete(OpcWorkspace))
            await session.commit()

    await clean()
    yield
    await clean()


async def _seed_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID = OWNER_ID,
    phone_e164: str = "+8613800098101",
) -> User:
    user = User(id=user_id, phone_e164=phone_e164)
    async with session_factory() as session:
        session.add(user)
        await session.commit()
    return user


@pytest.mark.asyncio
async def test_provision_creates_workspace_and_existing_owner_atomically(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed_user(auth_session_factory)

    result = await OpcProvisioningService(auth_session_factory).provision(
        opc_id="opc-provision-001",
        display_name="  Synthetic OPC  ",
        owner_user_id=owner.id,
    )

    assert result.status == "created"
    assert result.opc_id == "opc-provision-001"
    assert result.owner_user_id == owner.id
    assert result.role == "owner"
    async with auth_session_factory() as session:
        workspace = await session.get(OpcWorkspace, "opc-provision-001")
        membership = await session.scalar(
            select(OpcMembership).where(
                OpcMembership.opc_id == "opc-provision-001",
                OpcMembership.user_id == owner.id,
            )
        )
    assert workspace is not None
    assert workspace.display_name == "Synthetic OPC"
    assert membership is not None
    assert membership.role == "owner"


@pytest.mark.asyncio
async def test_exact_replay_is_unchanged_and_does_not_duplicate(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed_user(auth_session_factory)
    service = OpcProvisioningService(auth_session_factory)
    await service.provision(
        opc_id="opc-provision-001",
        display_name="Synthetic OPC",
        owner_user_id=owner.id,
    )

    result = await service.provision(
        opc_id="opc-provision-001",
        display_name="Synthetic OPC",
        owner_user_id=owner.id,
    )

    assert result.status == "unchanged"
    async with auth_session_factory() as session:
        workspace_count = await session.scalar(
            select(func.count()).select_from(OpcWorkspace)
        )
        membership_count = await session.scalar(
            select(func.count()).select_from(OpcMembership)
        )
    assert workspace_count == 1
    assert membership_count == 1


@pytest.mark.asyncio
async def test_missing_owner_fails_without_partial_workspace(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = OpcProvisioningService(auth_session_factory)

    with pytest.raises(OpcProvisioningError) as captured:
        await service.provision(
            opc_id="opc-provision-001",
            display_name="Synthetic OPC",
            owner_user_id=OWNER_ID,
        )

    assert captured.value.code == "owner_user_not_found"
    async with auth_session_factory() as session:
        workspace_count = await session.scalar(
            select(func.count()).select_from(OpcWorkspace)
        )
        membership_count = await session.scalar(
            select(func.count()).select_from(OpcMembership)
        )
    assert workspace_count == 0
    assert membership_count == 0


@pytest.mark.asyncio
async def test_display_name_conflict_does_not_replace_initialized_workspace(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed_user(auth_session_factory)
    service = OpcProvisioningService(auth_session_factory)
    await service.provision(
        opc_id="opc-provision-001",
        display_name="Synthetic OPC",
        owner_user_id=owner.id,
    )

    with pytest.raises(OpcProvisioningError) as captured:
        await service.provision(
            opc_id="opc-provision-001",
            display_name="Replacement OPC",
            owner_user_id=owner.id,
        )

    assert captured.value.code == "opc_display_name_conflict"
    async with auth_session_factory() as session:
        workspace = await session.get(OpcWorkspace, "opc-provision-001")
        membership_count = await session.scalar(
            select(func.count()).select_from(OpcMembership)
        )
    assert workspace is not None
    assert workspace.display_name == "Synthetic OPC"
    assert membership_count == 1


@pytest.mark.asyncio
async def test_existing_workspace_cannot_be_claimed_by_a_different_owner(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_owner = await _seed_user(auth_session_factory)
    second_owner = await _seed_user(
        auth_session_factory,
        user_id=UUID("00000000-0000-0000-0000-000000000102"),
        phone_e164="+8613800098102",
    )
    service = OpcProvisioningService(auth_session_factory)
    await service.provision(
        opc_id="opc-provision-001",
        display_name="Synthetic OPC",
        owner_user_id=first_owner.id,
    )

    with pytest.raises(OpcProvisioningError) as captured:
        await service.provision(
            opc_id="opc-provision-001",
            display_name="Synthetic OPC",
            owner_user_id=second_owner.id,
        )

    assert captured.value.code == "opc_owner_conflict"
    async with auth_session_factory() as session:
        memberships = (
            await session.scalars(select(OpcMembership).order_by(OpcMembership.user_id))
        ).all()
    assert [(row.user_id, row.role) for row in memberships] == [
        (first_owner.id, "owner")
    ]


@pytest.mark.asyncio
async def test_reviewer_membership_cannot_be_promoted_by_replaying_initialization(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reviewer = await _seed_user(auth_session_factory)
    async with auth_session_factory() as session:
        session.add(OpcWorkspace(id="opc-provision-001", display_name="Synthetic OPC"))
        await session.flush()
        session.add(
            OpcMembership(
                opc_id="opc-provision-001",
                user_id=reviewer.id,
                role="reviewer",
            )
        )
        await session.commit()

    with pytest.raises(OpcProvisioningError) as captured:
        await OpcProvisioningService(auth_session_factory).provision(
            opc_id="opc-provision-001",
            display_name="Synthetic OPC",
            owner_user_id=reviewer.id,
        )

    assert captured.value.code == "opc_owner_conflict"
    async with auth_session_factory() as session:
        membership = await session.scalar(select(OpcMembership))
    assert membership is not None
    assert membership.role == "reviewer"


@pytest.mark.asyncio
async def test_concurrent_exact_initialization_creates_one_authority_pair(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed_user(auth_session_factory)
    service = OpcProvisioningService(auth_session_factory)

    results = await asyncio.gather(
        service.provision(
            opc_id="opc-provision-001",
            display_name="Synthetic OPC",
            owner_user_id=owner.id,
        ),
        service.provision(
            opc_id="opc-provision-001",
            display_name="Synthetic OPC",
            owner_user_id=owner.id,
        ),
    )

    assert sorted(result.status for result in results) == ["created", "unchanged"]
    async with auth_session_factory() as session:
        workspace_count = await session.scalar(
            select(func.count()).select_from(OpcWorkspace)
        )
        membership_count = await session.scalar(
            select(func.count()).select_from(OpcMembership)
        )
    assert workspace_count == 1
    assert membership_count == 1


@pytest.mark.asyncio
async def test_concurrent_competing_owners_leave_one_winner_and_one_conflict(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_owner = await _seed_user(auth_session_factory)
    second_owner = await _seed_user(
        auth_session_factory,
        user_id=UUID("00000000-0000-0000-0000-000000000102"),
        phone_e164="+8613800098102",
    )
    service = OpcProvisioningService(auth_session_factory)

    outcomes = await asyncio.gather(
        service.provision(
            opc_id="opc-provision-001",
            display_name="Synthetic OPC",
            owner_user_id=first_owner.id,
        ),
        service.provision(
            opc_id="opc-provision-001",
            display_name="Synthetic OPC",
            owner_user_id=second_owner.id,
        ),
        return_exceptions=True,
    )

    successes = [
        outcome for outcome in outcomes if not isinstance(outcome, BaseException)
    ]
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert [result.status for result in successes] == ["created"]
    assert len(failures) == 1
    assert isinstance(failures[0], OpcProvisioningError)
    assert failures[0].code == "opc_owner_conflict"
    async with auth_session_factory() as session:
        workspace_count = await session.scalar(
            select(func.count()).select_from(OpcWorkspace)
        )
        memberships = (await session.scalars(select(OpcMembership))).all()
    assert workspace_count == 1
    assert len(memberships) == 1
    assert memberships[0].user_id in {first_owner.id, second_owner.id}
    assert memberships[0].role == "owner"
