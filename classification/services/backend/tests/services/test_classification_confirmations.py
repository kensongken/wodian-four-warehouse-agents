from uuid import UUID

import pytest

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    ClassificationConfirmationReceipt,
    canonical_classification_sha256,
)
from product_engine.repositories.classifications import (
    AppendClassificationResult,
    ClassificationSourceAsset,
    ClassificationWriteContext,
)
from product_engine.services.classification_confirmations import (
    ClassificationConfirmationFailure,
    ClassificationConfirmationService,
    LocalClassificationReview,
    LocalClassificationReviewItem,
)

TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
MAIN_ASSET_ID = UUID("22222222-2222-4222-8222-222222222222")
GALLERY_ASSET_ID = UUID("33333333-3333-4333-8333-333333333333")


def command() -> ClassificationConfirmationCommand:
    return ClassificationConfirmationCommand(
        request_id=UUID("11111111-1111-4111-8111-111111111111"),
        product_code="BCNHDIY914",
        local_batch_id="BATCH-20260920033509-8C21C203",
        base_material_package_version=1,
        material_label="信息不足",
        product_form="earring",
        main_asset_id=MAIN_ASSET_ID,
        visual_observations=[],
        selling_point_candidates=[],
        inventory_status=None,
        price_status="pending",
        price_candidate=None,
        service_rule_summaries=[],
        review_source="local_trainer_review",
        assets=[
            ClassificationAssetConfirmation(
                asset_id=MAIN_ASSET_ID,
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            ),
            ClassificationAssetConfirmation(
                asset_id=GALLERY_ASSET_ID,
                position=1,
                role="gallery",
                sha256="b" * 64,
            ),
        ],
    )


def context(*, latest_version: int = 1) -> ClassificationWriteContext:
    return ClassificationWriteContext(
        product_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        latest_material_package_version=latest_version,
        requested_material_package_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        assets=(
            ClassificationSourceAsset(
                upstream_asset_id=MAIN_ASSET_ID,
                source_image_id=UUID("44444444-4444-4444-8444-444444444444"),
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            ),
            ClassificationSourceAsset(
                upstream_asset_id=GALLERY_ASSET_ID,
                source_image_id=UUID("55555555-5555-4555-8555-555555555555"),
                position=1,
                role="gallery",
                sha256="b" * 64,
            ),
        ),
    )


def local_review(
    *,
    product_code: str = "BCNHDIY914",
    second_status: str = "reviewed",
) -> LocalClassificationReview:
    return LocalClassificationReview(
        product_code=product_code,
        items=(
            LocalClassificationReviewItem(
                asset_id=MAIN_ASSET_ID,
                status="reviewed",
                material_label="信息不足",
                product_form="earring",
            ),
            LocalClassificationReviewItem(
                asset_id=GALLERY_ASSET_ID,
                status=second_status,
                material_label="信息不足",
                product_form="earring",
            ),
        ),
    )


class FakeRepository:
    def __init__(self, loaded_context: ClassificationWriteContext) -> None:
        self.loaded_context = loaded_context
        self.appended = []

    async def load_context(self, product_code: str, material_package_version: int):
        return self.loaded_context

    async def append(self, value, loaded_context, reviewer_ref):
        self.appended.append((value, loaded_context, reviewer_ref))
        digest = canonical_classification_sha256(value)
        receipt = ClassificationConfirmationReceipt(
            request_id=value.request_id,
            product_code=value.product_code,
            classification_version=1,
            classification_id=UUID("66666666-6666-4666-8666-666666666666"),
            content_sha256=digest,
            material_package_version=value.base_material_package_version,
            material_label=value.material_label,
            material_evidence="information_insufficient",
            disposition="created",
            downstream_status="not_sent",
        )
        return AppendClassificationResult(receipt=receipt, disposition="created")


def service(
    *,
    loaded_context: ClassificationWriteContext | None = None,
    review: LocalClassificationReview | None = None,
) -> tuple[ClassificationConfirmationService, FakeRepository]:
    repository = FakeRepository(loaded_context or context())
    return (
        ClassificationConfirmationService(
            repository=repository,
            reviewer_ref="local-operator",
            local_review_loader=lambda _batch_id: review or local_review(),
        ),
        repository,
    )


async def test_confirm_writes_version_one_without_downstream_delivery() -> None:
    confirmation_service, repository = service()

    receipt = await confirmation_service.confirm(command())

    assert receipt.classification_version == 1
    assert receipt.content_sha256 == canonical_classification_sha256(command())
    assert receipt.material_package_version == 1
    assert receipt.downstream_status == "not_sent"
    assert len(repository.appended) == 1


async def test_mixed_product_review_is_rejected() -> None:
    confirmation_service, _ = service(review=local_review(product_code="OTHER"))
    with pytest.raises(ClassificationConfirmationFailure, match="product_review_identity_mismatch"):
        await confirmation_service.confirm(command())


async def test_stale_material_package_is_rejected() -> None:
    confirmation_service, _ = service(loaded_context=context(latest_version=2))
    with pytest.raises(ClassificationConfirmationFailure, match="stale_material_package_version"):
        await confirmation_service.confirm(command())


@pytest.mark.parametrize(
    "changed_assets",
    [
        lambda assets: list(reversed(assets)),
        lambda assets: [
            assets[0],
            assets[1].model_copy(update={"sha256": "c" * 64}),
        ],
    ],
)
async def test_changed_asset_identity_is_rejected(changed_assets) -> None:
    confirmation_service, _ = service()
    changed = command().model_copy(update={"assets": changed_assets(command().assets)})
    with pytest.raises(ClassificationConfirmationFailure, match="classification_asset_mismatch"):
        await confirmation_service.confirm(changed)


async def test_pending_review_item_is_rejected() -> None:
    confirmation_service, _ = service(review=local_review(second_status="pending_review"))
    with pytest.raises(ClassificationConfirmationFailure, match="product_review_incomplete"):
        await confirmation_service.confirm(command())
