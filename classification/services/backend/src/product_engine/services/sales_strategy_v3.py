from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from product_engine.contracts.sales_strategy import SalesStrategyImageRef
from product_engine.contracts.sales_strategy_v2 import ConfirmedProductFact
from product_engine.contracts.sales_strategy_v3 import (
    MATERIAL_WARNINGS,
    SalesClaimBasis,
    SalesMaterialFactV3,
    SalesStrategyInputV3,
    canonical_sales_strategy_v3_sha256,
)
from product_engine.repositories.sales_handoffs_v3 import SalesV3Source


class SalesPreviewFailureV3(ValueError):
    pass


class SalesPreviewRepositoryV3(Protocol):
    async def load_latest_source(
        self,
        tenant_id: UUID,
        classification_id: UUID,
    ) -> SalesV3Source | None: ...

    async def append_preview(self, candidate: SalesStrategyInputV3) -> object: ...


class SalesPreviewPreparerV3:
    def __init__(self, repository: SalesPreviewRepositoryV3) -> None:
        self.repository = repository

    async def prepare(self, tenant_id: UUID, classification_id: UUID) -> object:
        source = await self.repository.load_latest_source(tenant_id, classification_id)
        if source is None:
            raise SalesPreviewFailureV3("classification_not_found")
        if not source.is_latest:
            raise SalesPreviewFailureV3("classification_not_latest")
        return await self.prepare_source(source)

    async def prepare_source(self, source: SalesV3Source) -> object:
        candidate = self._build_payload(source)
        return await self.repository.append_preview(candidate)

    @staticmethod
    def _build_payload(source: SalesV3Source) -> SalesStrategyInputV3:
        if not source.assets:
            raise SalesPreviewFailureV3("classification_ledger_conflict")
        basis = cast(SalesClaimBasis, source.claim_basis)
        if basis not in MATERIAL_WARNINGS:
            raise SalesPreviewFailureV3("material_evidence_provenance_missing")
        if basis != "information_insufficient" and not source.evidence_active:
            raise SalesPreviewFailureV3("material_evidence_invalid")
        material_value = (
            "信息不足" if basis == "information_insufficient" else source.material_label
        )
        unverified = ("材质待人工确认",) if basis == "information_insufficient" else ()
        value = SalesStrategyInputV3(
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
            material=SalesMaterialFactV3(
                value=material_value,
                claim_basis=basis,
                claim_version=source.version,
                warning_text=MATERIAL_WARNINGS[basis],
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
                "不得将来源等级改写为鉴定结论",
                "不得自动报价或对客发送",
            ),
            created_at=datetime(1970, 1, 1, tzinfo=UTC),
            content_sha256="0" * 64,
        )
        return value.model_copy(
            update={"content_sha256": canonical_sales_strategy_v3_sha256(value)}
        )
