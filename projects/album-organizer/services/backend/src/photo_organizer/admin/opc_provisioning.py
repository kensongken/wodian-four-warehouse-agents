"""Transactional creation of an OPC workspace and its first owner."""

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.categorization.contracts import StableId
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.user import User
from photo_organizer.repositories.unit_of_work import UnitOfWork


class OpcProvisioningInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opc_id: StableId
    display_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    ]
    owner_user_id: UUID


@dataclass(frozen=True)
class OpcProvisioningResult:
    status: Literal["created", "unchanged"]
    opc_id: str
    owner_user_id: UUID
    role: Literal["owner"] = "owner"


class OpcProvisioningError(Exception):
    """An expected fail-closed provisioning conflict."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OpcProvisioningService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def provision(
        self,
        *,
        opc_id: str,
        display_name: str,
        owner_user_id: UUID,
    ) -> OpcProvisioningResult:
        provision = OpcProvisioningInput.model_validate(
            {
                "opc_id": opc_id,
                "display_name": display_name,
                "owner_user_id": owner_user_id,
            }
        )
        async with UnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"opc-provision:{provision.opc_id}"},
            )
            owner = await session.get(User, provision.owner_user_id)
            if owner is None:
                raise OpcProvisioningError("owner_user_not_found")

            workspace = await session.get(OpcWorkspace, provision.opc_id)
            if workspace is None:
                workspace = OpcWorkspace(
                    id=provision.opc_id,
                    display_name=provision.display_name,
                )
                session.add(workspace)
                await session.flush()
                session.add(
                    OpcMembership(
                        opc_id=provision.opc_id,
                        user_id=owner.id,
                        role="owner",
                    )
                )
                await session.flush()
                return OpcProvisioningResult(
                    status="created",
                    opc_id=provision.opc_id,
                    owner_user_id=owner.id,
                )

            if workspace.display_name != provision.display_name:
                raise OpcProvisioningError("opc_display_name_conflict")
            membership = await session.scalar(
                select(OpcMembership).where(
                    OpcMembership.opc_id == provision.opc_id,
                    OpcMembership.user_id == owner.id,
                )
            )
            if membership is None or membership.role != "owner":
                raise OpcProvisioningError("opc_owner_conflict")
            return OpcProvisioningResult(
                status="unchanged",
                opc_id=provision.opc_id,
                owner_user_id=owner.id,
            )
