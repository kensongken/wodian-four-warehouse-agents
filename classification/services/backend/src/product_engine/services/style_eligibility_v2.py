from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    canonical_classification_sha256,
)
from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityFact,
    StyleEligibilityImageRef,
    StyleEligibilityInputV2,
    canonical_style_eligibility_v2_sha256,
)
from product_engine.repositories.style_handoffs import (
    PreparedStyleEligibility,
    StyleClassificationSource,
    StyleTransformAuthorizationView,
    classification_image_set_sha256,
)

Now = Callable[[], datetime]

__all__ = [
    "PreparedStyleEligibility",
    "StyleEligibilityFailure",
    "StyleEligibilityPreparer",
]


class StyleEligibilityFailure(ValueError):
    pass


class StyleEligibilityRepository(Protocol):
    async def load_latest_source(
        self, tenant_id: UUID, classification_id: UUID
    ) -> StyleClassificationSource | None: ...

    async def load_latest_authorization(
        self,
        tenant_id: UUID,
        classification_id: UUID,
        source_image_set_sha256: str,
    ) -> StyleTransformAuthorizationView | None: ...

    async def append_eligibility(
        self, candidate: StyleEligibilityInputV2
    ) -> PreparedStyleEligibility: ...


class StyleEligibilityPreparer:
    def __init__(
        self,
        repository: StyleEligibilityRepository,
        *,
        now: Now | None = None,
    ) -> None:
        self.repository = repository
        self.now = now or (lambda: datetime.now(UTC))

    async def prepare(self, tenant_id: UUID, classification_id: UUID) -> PreparedStyleEligibility:
        source = await self.repository.load_latest_source(tenant_id, classification_id)
        if source is None:
            raise StyleEligibilityFailure("classification_not_found")
        if not source.is_latest:
            raise StyleEligibilityFailure("classification_not_latest")
        if not source.assets:
            raise StyleEligibilityFailure("classification_ledger_conflict")
        if self._classification_digest(source) != source.content_sha256:
            raise StyleEligibilityFailure("classification_ledger_conflict")
        if classification_image_set_sha256(source.assets) != source.source_image_set_sha256:
            raise StyleEligibilityFailure("classification_ledger_conflict")
        authorization = await self.repository.load_latest_authorization(
            tenant_id, classification_id, source.source_image_set_sha256
        )
        return await self.repository.append_eligibility(self._build_payload(source, authorization))

    @staticmethod
    def _classification_digest(source: StyleClassificationSource) -> str:
        main_assets = [
            asset.asset_id
            for asset in source.assets
            if asset.source_image_id == source.main_source_image_id
        ]
        if len(main_assets) != 1:
            raise StyleEligibilityFailure("classification_ledger_conflict")
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

    def _build_payload(
        self,
        source: StyleClassificationSource,
        authorization: StyleTransformAuthorizationView | None,
    ) -> StyleEligibilityInputV2:
        main_images = [
            asset for asset in source.assets if asset.source_image_id == source.main_source_image_id
        ]
        if len(main_images) != 1:
            raise StyleEligibilityFailure("classification_ledger_conflict")
        images = tuple(
            StyleEligibilityImageRef(
                asset_id=asset.asset_id,
                sha256=asset.sha256,
                mime_type=asset.mime_type,
                role=asset.role,
            )
            for asset in source.assets
        )
        authorization_status: Literal["none", "active", "expired", "revoked"]
        if authorization is None:
            authorization_status = "none"
        elif authorization.revoked_at is not None:
            authorization_status = "revoked"
        elif authorization.expires_at <= self.now():
            authorization_status = "expired"
        else:
            authorization_status = "active"
        value = StyleEligibilityInputV2(
            tenant_id=source.tenant_id,
            product_code=source.product_code,
            eligibility_input_version=1,
            source_material_package_version=source.base_material_package_version,
            source_classification_id=source.classification_id,
            source_classification_version=source.version,
            source_classification_sha256=source.content_sha256,
            source_image_set_sha256=source.source_image_set_sha256,
            main_image=StyleEligibilityImageRef(
                asset_id=main_images[0].asset_id,
                sha256=main_images[0].sha256,
                mime_type=main_images[0].mime_type,
                role=main_images[0].role,
            ),
            images=images,
            material=StyleEligibilityFact(
                value=source.material_label,
                evidence=source.material_evidence,
            ),
            form=StyleEligibilityFact(
                value=source.product_form,
                evidence="human_confirmed",
            ),
            transform_authorization_id=(
                authorization.authorization_id if authorization is not None else None
            ),
            transform_authorization_status=authorization_status,
            transform_authorization_expires_at=(
                authorization.expires_at if authorization is not None else None
            ),
            transform_authorization_revoked_at=(
                authorization.revoked_at if authorization is not None else None
            ),
            created_at=datetime(1970, 1, 1, tzinfo=UTC),
            content_sha256="0" * 64,
        )
        return value.model_copy(
            update={"content_sha256": canonical_style_eligibility_v2_sha256(value)}
        )
