from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    ClassificationConfirmationReceipt,
)
from product_engine.repositories.classifications import (
    AppendClassificationResult,
    ClassificationSourceAsset,
    ClassificationWriteContext,
)


class ClassificationConfirmationFailure(ValueError):
    pass


@dataclass(frozen=True)
class LocalClassificationReviewItem:
    asset_id: UUID
    status: str
    material_label: str
    product_form: str


@dataclass(frozen=True)
class LocalClassificationReview:
    product_code: str
    items: tuple[LocalClassificationReviewItem, ...]


class ClassificationLedger(Protocol):
    async def load_context(
        self, product_code: str, material_package_version: int
    ) -> ClassificationWriteContext | None: ...

    async def append(
        self,
        command: ClassificationConfirmationCommand,
        context: ClassificationWriteContext,
        reviewer_ref: str,
    ) -> AppendClassificationResult: ...


def _command_assets(
    assets: list[ClassificationAssetConfirmation],
) -> tuple[tuple[UUID, int, str, str], ...]:
    return tuple((asset.asset_id, asset.position, asset.role, asset.sha256) for asset in assets)


def _context_assets(
    assets: tuple[ClassificationSourceAsset, ...],
) -> tuple[tuple[UUID, int, str, str], ...]:
    return tuple(
        (asset.upstream_asset_id, asset.position, asset.role, asset.sha256) for asset in assets
    )


class ClassificationConfirmationService:
    def __init__(
        self,
        *,
        repository: ClassificationLedger,
        reviewer_ref: str,
        local_review_loader: Callable[[str], LocalClassificationReview | None],
    ) -> None:
        self.repository = repository
        self.reviewer_ref = reviewer_ref
        self.local_review_loader = local_review_loader

    async def confirm(
        self, command: ClassificationConfirmationCommand
    ) -> ClassificationConfirmationReceipt:
        local_review = self.local_review_loader(command.local_batch_id)
        if local_review is None or command.product_code != local_review.product_code:
            raise ClassificationConfirmationFailure("product_review_identity_mismatch")
        if any(item.status != "reviewed" for item in local_review.items):
            raise ClassificationConfirmationFailure("product_review_incomplete")
        if not local_review.items:
            raise ClassificationConfirmationFailure("product_review_incomplete")

        local_materials = {item.material_label for item in local_review.items}
        local_forms = {item.product_form for item in local_review.items}
        if local_materials != {command.material_label} or local_forms != {command.product_form}:
            raise ClassificationConfirmationFailure("product_review_value_mismatch")

        context = await self.repository.load_context(
            command.product_code, command.base_material_package_version
        )
        if context is None:
            raise ClassificationConfirmationFailure("classification_context_not_found")
        if command.base_material_package_version != context.latest_material_package_version:
            raise ClassificationConfirmationFailure("stale_material_package_version")
        if _command_assets(command.assets) != _context_assets(context.assets):
            raise ClassificationConfirmationFailure("classification_asset_mismatch")

        result = await self.repository.append(command, context, self.reviewer_ref)
        return result.receipt
