from dataclasses import dataclass
from uuid import UUID

import pytest

from product_engine.contracts.material_evidence import (
    MaterialEvidenceReceipt,
    MaterialEvidenceRegistrationCommand,
)
from product_engine.repositories.material_evidence import (
    MaterialEvidenceActorContext,
    MaterialEvidenceFailure,
)
from product_engine.services.material_evidence import MaterialEvidenceService

REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")
OLD_ID = UUID("22222222-2222-4222-8222-222222222222")
LATEST_ID = UUID("33333333-3333-4333-8333-333333333333")


def registration(**updates: object) -> MaterialEvidenceRegistrationCommand:
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "product_code": "BCNHDIY914",
        "base_classification_id": OLD_ID,
        "material_label": "南红",
        "claim_basis": "merchant_declared",
        "evidence_kind": "merchant_declaration",
        "evidence_ref": "material-declaration:unit-test",
        "evidence_sha256": None,
        "source_summary": "商家确认当前商品材质为南红",
        "acknowledge_not_authentication": True,
    }
    values.update(updates)
    return MaterialEvidenceRegistrationCommand.model_validate(values)


def actor_context() -> MaterialEvidenceActorContext:
    return MaterialEvidenceActorContext(
        declared_by_ref="merchant-local-01",
        reviewer_ref="trainer-local-01",
    )


@dataclass
class FakeMaterialEvidenceRepository:
    latest_classification_id: UUID

    async def register(
        self,
        command: MaterialEvidenceRegistrationCommand,
        actor: MaterialEvidenceActorContext,
    ) -> MaterialEvidenceReceipt:
        del actor
        if command.base_classification_id != self.latest_classification_id:
            raise MaterialEvidenceFailure("stale_classification")
        raise AssertionError("test fixture expected a stale command")


async def test_registration_rejects_a_stale_base_classification() -> None:
    repository = FakeMaterialEvidenceRepository(latest_classification_id=LATEST_ID)
    service = MaterialEvidenceService(repository, actor_context())

    with pytest.raises(MaterialEvidenceFailure, match="stale_classification"):
        await service.register(registration(base_classification_id=OLD_ID))
