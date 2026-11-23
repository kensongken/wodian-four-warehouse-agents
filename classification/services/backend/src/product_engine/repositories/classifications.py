import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from product_engine.contracts.classification import (
    ClassificationConfirmationCommand,
    ClassificationConfirmationReceipt,
    canonical_classification_sha256,
)
from product_engine.models.classifications import (
    ProductClassificationAsset,
    ProductClassificationVersion,
)
from product_engine.models.products import (
    MaterialPackage,
    MaterialPackageAsset,
    Product,
    SourceImage,
)


class ClassificationLedgerFailure(ValueError):
    pass


@dataclass(frozen=True)
class ClassificationSourceAsset:
    upstream_asset_id: UUID
    source_image_id: UUID
    position: int
    role: str
    sha256: str


@dataclass(frozen=True)
class ClassificationWriteContext:
    product_id: UUID
    latest_material_package_version: int
    requested_material_package_id: UUID
    assets: tuple[ClassificationSourceAsset, ...]


@dataclass(frozen=True)
class AppendClassificationResult:
    receipt: ClassificationConfirmationReceipt
    disposition: Literal["created", "duplicate"]


def locked_product_statement(tenant_id: UUID, product_id: UUID) -> Select[tuple[UUID]]:
    return (
        select(Product.id)
        .where(Product.tenant_id == tenant_id, Product.id == product_id)
        .with_for_update()
    )


def classification_by_request_statement(
    tenant_id: UUID, request_id: UUID
) -> Select[tuple[ProductClassificationVersion]]:
    return select(ProductClassificationVersion).where(
        ProductClassificationVersion.tenant_id == tenant_id,
        ProductClassificationVersion.request_id == request_id,
    )


def classification_by_content_statement(
    tenant_id: UUID, product_id: UUID, content_sha256: str
) -> Select[tuple[ProductClassificationVersion]]:
    return select(ProductClassificationVersion).where(
        ProductClassificationVersion.tenant_id == tenant_id,
        ProductClassificationVersion.product_id == product_id,
        ProductClassificationVersion.content_sha256 == content_sha256,
    )


def latest_classification_statement(
    tenant_id: UUID, product_id: UUID
) -> Select[tuple[ProductClassificationVersion]]:
    return (
        select(ProductClassificationVersion)
        .where(
            ProductClassificationVersion.tenant_id == tenant_id,
            ProductClassificationVersion.product_id == product_id,
        )
        .order_by(ProductClassificationVersion.version.desc())
        .limit(1)
    )


def _image_set_sha256(command: ClassificationConfirmationCommand) -> str:
    body = [asset.model_dump(mode="json") for asset in command.assets]
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class ClassificationLedgerRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def load_context(
        self,
        product_code: str,
        material_package_version: int,
    ) -> ClassificationWriteContext | None:
        product = (
            await self.session.execute(
                select(Product).where(
                    Product.tenant_id == self.tenant_id,
                    Product.product_code == product_code,
                )
            )
        ).scalar_one_or_none()
        if product is None:
            return None

        latest_version = (
            await self.session.execute(
                select(func.max(MaterialPackage.version)).where(
                    MaterialPackage.tenant_id == self.tenant_id,
                    MaterialPackage.product_id == product.id,
                )
            )
        ).scalar_one_or_none()
        requested = (
            await self.session.execute(
                select(MaterialPackage).where(
                    MaterialPackage.tenant_id == self.tenant_id,
                    MaterialPackage.product_id == product.id,
                    MaterialPackage.version == material_package_version,
                )
            )
        ).scalar_one_or_none()
        if latest_version is None or requested is None:
            return None

        rows = (
            await self.session.execute(
                select(
                    SourceImage.upstream_asset_id,
                    SourceImage.id,
                    MaterialPackageAsset.position,
                    SourceImage.role,
                    SourceImage.sha256,
                )
                .join(
                    SourceImage,
                    SourceImage.id == MaterialPackageAsset.source_image_id,
                )
                .where(
                    MaterialPackageAsset.tenant_id == self.tenant_id,
                    MaterialPackageAsset.package_id == requested.id,
                    SourceImage.tenant_id == self.tenant_id,
                )
                .order_by(MaterialPackageAsset.position)
            )
        ).all()
        return ClassificationWriteContext(
            product_id=product.id,
            latest_material_package_version=latest_version,
            requested_material_package_id=requested.id,
            assets=tuple(
                ClassificationSourceAsset(
                    upstream_asset_id=row[0],
                    source_image_id=row[1],
                    position=row[2],
                    role=row[3],
                    sha256=row[4],
                )
                for row in rows
            ),
        )

    async def append(
        self,
        command: ClassificationConfirmationCommand,
        context: ClassificationWriteContext,
        reviewer_ref: str,
    ) -> AppendClassificationResult:
        await self.session.execute(locked_product_statement(self.tenant_id, context.product_id))
        digest = canonical_classification_sha256(command)

        by_request = await self._by_request(command.request_id)
        if by_request is not None:
            if by_request.content_sha256 != digest:
                raise ClassificationLedgerFailure("classification_request_conflict")
            return self._result(by_request, command.product_code, "duplicate")

        by_content = await self._by_content(context.product_id, digest)
        if by_content is not None:
            return self._result(by_content, command.product_code, "duplicate")

        latest = await self.latest(context.product_id)
        version = 1 if latest is None else latest.version + 1
        classification_id = uuid4()
        reviewed_at = datetime.now(UTC)
        record = ProductClassificationVersion(
            id=classification_id,
            tenant_id=self.tenant_id,
            product_id=context.product_id,
            version=version,
            base_material_package_id=context.requested_material_package_id,
            base_material_package_version=command.base_material_package_version,
            material_label=command.material_label,
            product_form=command.product_form,
            main_asset_id=self._source_image_id(command.main_asset_id, context),
            visual_observations=command.visual_observations,
            selling_point_candidates=command.selling_point_candidates,
            inventory_status=command.inventory_status,
            price_status=command.price_status,
            price_candidate=command.price_candidate,
            service_rule_summaries=command.service_rule_summaries,
            source_image_set_sha256=_image_set_sha256(command),
            review_status="human_confirmed",
            material_evidence=self._material_evidence(command.material_label),
            review_source=command.review_source,
            reviewer_ref=reviewer_ref,
            reviewed_at=reviewed_at,
            request_id=command.request_id,
            content_sha256=digest,
        )
        assets_by_upstream = {asset.upstream_asset_id: asset for asset in context.assets}
        asset_rows = [
            ProductClassificationAsset(
                id=uuid4(),
                tenant_id=self.tenant_id,
                classification_version_id=classification_id,
                source_image_id=assets_by_upstream[asset.asset_id].source_image_id,
                position=asset.position,
                role=asset.role,
                sha256=asset.sha256,
            )
            for asset in command.assets
        ]

        try:
            async with self.session.begin_nested():
                self.session.add(record)
                await self.session.flush()
                self.session.add_all(asset_rows)
                await self.session.flush()
        except IntegrityError as error:
            recovered = await self._recover_uniqueness(command, context.product_id, digest)
            if recovered is not None:
                return recovered
            raise ClassificationLedgerFailure("classification_ledger_conflict") from error

        return self._result(record, command.product_code, "created")

    async def latest(self, product_id: UUID) -> ProductClassificationVersion | None:
        return (
            await self.session.execute(latest_classification_statement(self.tenant_id, product_id))
        ).scalar_one_or_none()

    async def _by_request(self, request_id: UUID) -> ProductClassificationVersion | None:
        return (
            await self.session.execute(
                classification_by_request_statement(self.tenant_id, request_id)
            )
        ).scalar_one_or_none()

    async def _by_content(
        self, product_id: UUID, digest: str
    ) -> ProductClassificationVersion | None:
        return (
            await self.session.execute(
                classification_by_content_statement(self.tenant_id, product_id, digest)
            )
        ).scalar_one_or_none()

    async def _recover_uniqueness(
        self,
        command: ClassificationConfirmationCommand,
        product_id: UUID,
        digest: str,
    ) -> AppendClassificationResult | None:
        by_request = await self._by_request(command.request_id)
        if by_request is not None:
            if by_request.content_sha256 != digest:
                raise ClassificationLedgerFailure("classification_request_conflict")
            return self._result(by_request, command.product_code, "duplicate")
        by_content = await self._by_content(product_id, digest)
        if by_content is not None:
            return self._result(by_content, command.product_code, "duplicate")
        return None

    @staticmethod
    def _source_image_id(upstream_asset_id: UUID, context: ClassificationWriteContext) -> UUID:
        for asset in context.assets:
            if asset.upstream_asset_id == upstream_asset_id:
                return asset.source_image_id
        raise ClassificationLedgerFailure("classification_asset_mismatch")

    @staticmethod
    def _material_evidence(
        material_label: str,
    ) -> Literal["human_confirmed", "information_insufficient"]:
        return "information_insufficient" if material_label == "信息不足" else "human_confirmed"

    def _result(
        self,
        record: ProductClassificationVersion,
        product_code: str,
        disposition: Literal["created", "duplicate"],
    ) -> AppendClassificationResult:
        receipt = ClassificationConfirmationReceipt(
            request_id=record.request_id,
            product_code=product_code,
            classification_version=record.version,
            classification_id=record.id,
            content_sha256=record.content_sha256,
            material_package_version=record.base_material_package_version,
            material_label=record.material_label,
            material_evidence=self._material_evidence(record.material_label),
            disposition=disposition,
            downstream_status="not_sent",
        )
        return AppendClassificationResult(receipt=receipt, disposition=disposition)
