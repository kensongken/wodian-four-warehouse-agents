from datetime import UTC, datetime
from uuid import UUID

import pytest

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    canonical_classification_sha256,
)
from product_engine.repositories.sales_handoffs import (
    AuthoritativeClassificationAsset,
    AuthoritativeClassificationSource,
    classification_image_set_sha256,
)
from product_engine.services.sales_strategy_v2 import (
    PreparedSalesPreview,
    SalesPreviewFailure,
    SalesPreviewPreparer,
    WarehouseHandoffView,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
PRODUCT_ID = UUID("33333333-3333-4333-8333-333333333333")
CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")
OLD_CLASSIFICATION_ID = UUID("55555555-5555-4555-8555-555555555555")
MAIN_ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
MAIN_SOURCE_ID = UUID("99999999-9999-4999-8999-999999999999")
HANDOFF_ID = UUID("77777777-7777-4777-8777-777777777777")


def classification_source(
    *,
    classification_id: UUID = CLASSIFICATION_ID,
    is_latest: bool = True,
    material_label: str = "信息不足",
    digest: str | None = None,
) -> AuthoritativeClassificationSource:
    command = ClassificationConfirmationCommand(
        request_id="11111111-1111-4111-8111-111111111111",
        product_code="BCNHDIY914",
        local_batch_id="BATCH-TEST",
        base_material_package_version=1,
        material_label=material_label,
        product_form="earring",
        main_asset_id=MAIN_ASSET_ID,
        visual_observations=["金色圆环外形"],
        selling_point_candidates=["简洁日常佩戴"],
        inventory_status=None,
        price_status="pending",
        price_candidate=None,
        service_rule_summaries=["需人工确认材质后报价"],
        review_source="local_trainer_review",
        assets=[
            ClassificationAssetConfirmation(
                asset_id=MAIN_ASSET_ID,
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            )
        ],
    )
    assets = (
        AuthoritativeClassificationAsset(
            asset_id=MAIN_ASSET_ID,
            source_image_id=MAIN_SOURCE_ID,
            position=0,
            role="main_candidate",
            sha256="a" * 64,
            mime_type="image/jpeg",
        ),
    )
    return AuthoritativeClassificationSource(
        classification_id=classification_id,
        tenant_id=TENANT_ID,
        product_id=PRODUCT_ID,
        product_code="BCNHDIY914",
        version=1,
        is_latest=is_latest,
        base_material_package_version=1,
        material_label=material_label,
        material_evidence=(
            "information_insufficient" if material_label == "信息不足" else "human_confirmed"
        ),
        product_form="earring",
        main_source_image_id=MAIN_SOURCE_ID,
        visual_observations=("金色圆环外形",),
        selling_point_candidates=("简洁日常佩戴",),
        inventory_status=None,
        price_status="pending",
        price_candidate=None,
        service_rule_summaries=("需人工确认材质后报价",),
        review_source="local_trainer_review",
        request_id=command.request_id,
        content_sha256=digest or canonical_classification_sha256(command),
        source_image_set_sha256=classification_image_set_sha256(assets),
        assets=assets,
    )


class FakeRepository:
    def __init__(self, source: AuthoritativeClassificationSource | None) -> None:
        self.source = source
        self.appended = []

    async def load_latest_source(self, tenant_id: UUID, classification_id: UUID):
        if (
            self.source is None
            or tenant_id != self.source.tenant_id
            or classification_id != self.source.classification_id
        ):
            return None
        return self.source

    async def append_preview(self, candidate):
        self.appended.append(candidate)
        return PreparedSalesPreview(
            payload=candidate,
            handoff=WarehouseHandoffView(
                handoff_id=HANDOFF_ID,
                product_code=candidate.product_code,
                target_warehouse="sales",
                payload_version=candidate.strategy_input_version,
                payload_sha256=candidate.content_sha256,
                idempotency_key=(
                    f"sales:{candidate.tenant_id}:{candidate.source_classification_id}"
                ),
                status="prepared",
                receiver_receipt_id=None,
                rejection_code=None,
                prepared_at=datetime(2026, 9, 20, 8, tzinfo=UTC),
                last_attempted_at=None,
                settled_at=None,
            ),
            created=True,
        )


async def test_prepare_uses_latest_classification_and_adds_material_warning() -> None:
    repository = FakeRepository(classification_source())

    result = await SalesPreviewPreparer(repository).prepare(TENANT_ID, CLASSIFICATION_ID)

    assert result.payload.source_classification_id == CLASSIFICATION_ID
    assert result.payload.form.value == "earring"
    assert result.payload.material.evidence == "information_insufficient"
    assert "材质待人工确认" in result.payload.unverified_items
    assert result.payload.visual_observations == ("金色圆环外形",)
    assert result.payload.selling_point_candidates == ("简洁日常佩戴",)
    assert result.handoff.status == "prepared"


async def test_prepare_rejects_a_non_latest_classification() -> None:
    repository = FakeRepository(
        classification_source(classification_id=OLD_CLASSIFICATION_ID, is_latest=False)
    )

    with pytest.raises(SalesPreviewFailure, match="classification_not_latest"):
        await SalesPreviewPreparer(repository).prepare(TENANT_ID, OLD_CLASSIFICATION_ID)

    assert repository.appended == []


async def test_prepare_rejects_a_source_digest_mismatch() -> None:
    repository = FakeRepository(classification_source(digest="f" * 64))

    with pytest.raises(SalesPreviewFailure, match="classification_ledger_conflict"):
        await SalesPreviewPreparer(repository).prepare(TENANT_ID, CLASSIFICATION_ID)

    assert repository.appended == []


async def test_prepare_rejects_cross_tenant_or_missing_classification() -> None:
    repository = FakeRepository(classification_source())

    with pytest.raises(SalesPreviewFailure, match="classification_not_found"):
        await SalesPreviewPreparer(repository).prepare(UUID(int=9), CLASSIFICATION_ID)
