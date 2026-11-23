from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    canonical_classification_sha256,
)
from product_engine.contracts.sales_strategy import SalesStrategyImageRef
from product_engine.contracts.sales_strategy_v2 import (
    ConfirmedProductFact,
    SalesStrategyInputV2,
    canonical_sales_strategy_v2_sha256,
)
from product_engine.repositories.sales_handoffs import (
    AuthoritativeClassificationSource,
    PreparedSalesPreview,
    WarehouseHandoffView,
    classification_image_set_sha256,
)

__all__ = [
    "PreparedSalesPreview",
    "SalesPreviewFailure",
    "SalesPreviewPreparer",
    "WarehouseHandoffView",
]


class SalesPreviewFailure(ValueError):
    pass


class SalesPreviewRepository(Protocol):
    async def load_latest_source(
        self, tenant_id: UUID, classification_id: UUID
    ) -> AuthoritativeClassificationSource | None: ...

    async def append_preview(self, candidate: SalesStrategyInputV2) -> PreparedSalesPreview: ...


class SalesPreviewPreparer:
    def __init__(self, repository: SalesPreviewRepository) -> None:
        self.repository = repository

    async def prepare(self, tenant_id: UUID, classification_id: UUID) -> PreparedSalesPreview:
        source = await self.repository.load_latest_source(tenant_id, classification_id)
        if source is None:
            raise SalesPreviewFailure("classification_not_found")
        if not source.is_latest:
            raise SalesPreviewFailure("classification_not_latest")
        if not source.assets:
            raise SalesPreviewFailure("classification_ledger_conflict")
        if self._classification_digest(source) != source.content_sha256:
            raise SalesPreviewFailure("classification_ledger_conflict")
        if classification_image_set_sha256(source.assets) != source.source_image_set_sha256:
            raise SalesPreviewFailure("classification_ledger_conflict")
        return await self.repository.append_preview(self._build_payload(source))

    @staticmethod
    def _classification_digest(source: AuthoritativeClassificationSource) -> str:
        main_assets = [
            asset.asset_id
            for asset in source.assets
            if asset.source_image_id == source.main_source_image_id
        ]
        if len(main_assets) != 1:
            raise SalesPreviewFailure("classification_ledger_conflict")
        command = ClassificationConfirmationCommand(
            request_id=source.request_id,
            product_code=source.product_code,
            local_batch_id="ledger-revalidation",
            base_material_package_version=source.base_material_package_version,
            material_label=source.material_label,
            product_form=source.product_form,
            main_asset_id=main_assets[0],
            visual_observations=list(source.visual_observations),
            selling_point_candidates=list(source.selling_point_candidates),
            inventory_status=source.inventory_status,
            price_status=source.price_status,
            price_candidate=source.price_candidate,
            service_rule_summaries=list(source.service_rule_summaries),
            review_source=source.review_source,
            assets=[
                ClassificationAssetConfirmation(
                    asset_id=asset.asset_id,
                    position=asset.position,
                    role=asset.role,
                    sha256=asset.sha256,
                )
                for asset in source.assets
            ],
        )
        return canonical_classification_sha256(command)

    @staticmethod
    def _build_payload(source: AuthoritativeClassificationSource) -> SalesStrategyInputV2:
        unverified = (
            ("材质待人工确认",) if source.material_evidence == "information_insufficient" else ()
        )
        value = SalesStrategyInputV2(
            tenant_id=source.tenant_id,
            product_code=source.product_code,
            strategy_input_version=1,
            source_material_package_version=source.base_material_package_version,
            source_classification_id=source.classification_id,
            source_classification_version=source.version,
            source_classification_sha256=source.content_sha256,
            source_image_set_sha256=source.source_image_set_sha256,
            images=tuple(
                SalesStrategyImageRef(
                    asset_id=asset.asset_id,
                    material_id=asset.source_image_id,
                    sha256=asset.sha256,
                    mime_type=asset.mime_type,
                    role=asset.role,
                )
                for asset in source.assets
            ),
            material=ConfirmedProductFact(
                value=source.material_label,
                evidence=source.material_evidence,
            ),
            form=ConfirmedProductFact(
                value=source.product_form,
                evidence="human_confirmed",
            ),
            inventory_status=source.inventory_status,
            visual_observations=source.visual_observations,
            selling_point_candidates=source.selling_point_candidates,
            price_status=source.price_status,
            price_candidate=source.price_candidate,
            service_rule_summaries=source.service_rule_summaries,
            unverified_items=unverified,
            forbidden_expressions=(
                "不得将未确认材质表述为事实",
                "不得自动报价或对客发送",
            ),
            created_at=datetime(1970, 1, 1, tzinfo=UTC),
            content_sha256="0" * 64,
        )
        return value.model_copy(
            update={"content_sha256": canonical_sales_strategy_v2_sha256(value)}
        )
