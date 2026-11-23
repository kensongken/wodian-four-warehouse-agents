from typing import Protocol

from product_engine.contracts.material_evidence import (
    MaterialEvidenceInsufficientCommand,
    MaterialEvidenceInsufficientReceipt,
    MaterialEvidenceReceipt,
    MaterialEvidenceRegistrationCommand,
    MaterialEvidenceRevocationCommand,
    MaterialEvidenceRevocationReceipt,
)
from product_engine.repositories.material_evidence import MaterialEvidenceActorContext


class MaterialEvidenceRegistrationRepository(Protocol):
    async def register(
        self,
        command: MaterialEvidenceRegistrationCommand,
        actor: MaterialEvidenceActorContext,
    ) -> MaterialEvidenceReceipt: ...

    async def keep_insufficient(
        self,
        command: MaterialEvidenceInsufficientCommand,
    ) -> MaterialEvidenceInsufficientReceipt: ...

    async def revoke(
        self,
        command: MaterialEvidenceRevocationCommand,
        actor: MaterialEvidenceActorContext,
    ) -> MaterialEvidenceRevocationReceipt: ...


class MaterialEvidenceService:
    def __init__(
        self,
        repository: MaterialEvidenceRegistrationRepository,
        actor: MaterialEvidenceActorContext,
    ) -> None:
        self.repository = repository
        self.actor = actor

    async def register(
        self,
        command: MaterialEvidenceRegistrationCommand,
    ) -> MaterialEvidenceReceipt:
        return await self.repository.register(command, self.actor)

    async def keep_insufficient(
        self,
        command: MaterialEvidenceInsufficientCommand,
    ) -> MaterialEvidenceInsufficientReceipt:
        return await self.repository.keep_insufficient(command)

    async def revoke(
        self,
        command: MaterialEvidenceRevocationCommand,
    ) -> MaterialEvidenceRevocationReceipt:
        return await self.repository.revoke(command, self.actor)
