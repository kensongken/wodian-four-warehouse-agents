from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityFact,
    StyleEligibilityImageRef,
)
from product_engine.contracts.style_warehouse_v3 import (
    StyleEligibilityInputV3,
    StyleMaterialFactV3,
    canonical_style_eligibility_v3_sha256,
)
from product_engine.repositories.style_handoffs import StyleTransformAuthorizationView
from product_engine.repositories.style_handoffs_v3 import StyleV3Source

Now = Callable[[], datetime]


class StyleEligibilityFailureV3(ValueError):
    pass


class StyleEligibilityRepositoryV3(Protocol):
    async def load_latest_source(
        self, tenant_id: UUID, classification_id: UUID
    ) -> StyleV3Source | None: ...
    async def load_latest_authorization(
        self, tenant_id: UUID, classification_id: UUID, source_image_set_sha256: str
    ) -> StyleTransformAuthorizationView | None: ...
    async def append_eligibility(self, candidate: StyleEligibilityInputV3) -> object: ...


class StyleEligibilityPreparerV3:
    def __init__(self, repository: StyleEligibilityRepositoryV3, *, now: Now | None = None) -> None:
        self.repository = repository
        self.now = now or (lambda: datetime.now(UTC))

    async def prepare(self, tenant_id: UUID, classification_id: UUID) -> object:
        source = await self.repository.load_latest_source(tenant_id, classification_id)
        if source is None:
            raise StyleEligibilityFailureV3("classification_not_found")
        if not source.is_latest:
            raise StyleEligibilityFailureV3("classification_not_latest")
        if (
            source.claim_basis not in {"document_supported", "merchant_declared"}
            or source.evidence_id is None
            or source.evidence_content_sha256 is None
        ):
            raise StyleEligibilityFailureV3("material_evidence_provenance_missing")
        if not source.evidence_active:
            raise StyleEligibilityFailureV3("material_evidence_invalid")
        if not source.assets:
            raise StyleEligibilityFailureV3("classification_ledger_conflict")
        authorization = await self.repository.load_latest_authorization(
            tenant_id, classification_id, source.source_image_set_sha256
        )
        return await self.repository.append_eligibility(self._build(source, authorization))

    def _build(
        self, source: StyleV3Source, authorization: StyleTransformAuthorizationView | None
    ) -> StyleEligibilityInputV3:
        images = tuple(
            StyleEligibilityImageRef(
                asset_id=a.asset_id, sha256=a.sha256, mime_type=a.mime_type, role=a.role
            )
            for a in source.assets
        )
        main = next(
            (
                a
                for a in images
                if a.asset_id
                == next(
                    x.asset_id
                    for x in source.assets
                    if x.source_image_id == source.main_source_image_id
                )
            ),
            None,
        )
        if main is None:
            raise StyleEligibilityFailureV3("classification_ledger_conflict")
        if authorization is None:
            auth_status = "none"
        elif authorization.revoked_at is not None:
            auth_status = "revoked"
        elif authorization.expires_at <= self.now():
            auth_status = "expired"
        else:
            auth_status = "active"
        value = StyleEligibilityInputV3(
            tenant_id=source.tenant_id,
            product_code=source.product_code,
            eligibility_input_version=1,
            source_material_package_version=source.base_material_package_version,
            source_classification_id=source.classification_id,
            source_classification_version=source.version,
            source_classification_sha256=source.content_sha256,
            source_image_set_sha256=source.source_image_set_sha256,
            main_image=main,
            images=images,
            material=StyleMaterialFactV3(
                value=source.material_label,
                claim_basis=cast(
                    Literal["document_supported", "merchant_declared"],
                    source.claim_basis,
                ),
                evidence_record_id=source.evidence_id,
                evidence_content_sha256=source.evidence_content_sha256,
            ),
            form=StyleEligibilityFact(value=source.product_form, evidence="human_confirmed"),
            transform_authorization_id=authorization.authorization_id if authorization else None,
            transform_authorization_status=cast(
                Literal["none", "active", "expired", "revoked"], auth_status
            ),
            transform_authorization_expires_at=authorization.expires_at if authorization else None,
            transform_authorization_revoked_at=authorization.revoked_at if authorization else None,
            created_at=datetime(1970, 1, 1, tzinfo=UTC),
            content_sha256="0" * 64,
        )
        return value.model_copy(
            update={"content_sha256": canonical_style_eligibility_v3_sha256(value)}
        )
