from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from product_engine.repositories.style_handoffs_v3 import StyleV3Asset, StyleV3Source
from product_engine.services.style_eligibility_v3 import (
    StyleEligibilityFailureV3,
    StyleEligibilityPreparerV3,
)

TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CLASSIFICATION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
EVIDENCE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
NOW = datetime(2026, 9, 21, tzinfo=UTC)


def source(**updates: object) -> StyleV3Source:
    image_id = uuid4()
    values = {
        "classification_id": CLASSIFICATION_ID,
        "tenant_id": TENANT_ID,
        "product_id": uuid4(),
        "product_code": "BCNHDIY914",
        "version": 2,
        "is_latest": True,
        "base_material_package_version": 1,
        "material_label": "绿松石",
        "claim_basis": "merchant_declared",
        "evidence_id": EVIDENCE_ID,
        "evidence_content_sha256": "e" * 64,
        "evidence_active": True,
        "product_form": "earring",
        "main_source_image_id": image_id,
        "content_sha256": "a" * 64,
        "source_image_set_sha256": "b" * 64,
        "assets": (StyleV3Asset(uuid4(), image_id, 0, "main_candidate", "c" * 64, "image/jpeg"),),
    }
    values.update(updates)
    return StyleV3Source(**values)  # type: ignore[arg-type]


class Repository:
    def __init__(self, value: StyleV3Source) -> None:
        self.value = value
        self.candidate = None

    async def load_latest_source(self, tenant_id, classification_id):
        return self.value

    async def load_latest_authorization(self, tenant_id, classification_id, image_set):
        return None

    async def append_eligibility(self, candidate):
        self.candidate = candidate
        return candidate


async def test_preparer_carries_evidence_provenance_without_generation() -> None:
    repository = Repository(source())
    value = await StyleEligibilityPreparerV3(repository, now=lambda: NOW).prepare(
        TENANT_ID, CLASSIFICATION_ID
    )

    assert value.material.claim_basis == "merchant_declared"
    assert value.material.evidence_record_id == EVIDENCE_ID
    assert value.automatic_generation_allowed is False


@pytest.mark.parametrize("updates", [{"claim_basis": None}, {"evidence_active": False}])
async def test_preparer_rejects_missing_or_inactive_provenance(updates: dict[str, object]) -> None:
    with pytest.raises(StyleEligibilityFailureV3):
        await StyleEligibilityPreparerV3(Repository(source(**updates))).prepare(
            TENANT_ID, CLASSIFICATION_ID
        )
