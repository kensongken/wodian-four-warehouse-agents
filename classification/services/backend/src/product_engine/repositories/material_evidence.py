import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from product_engine.contracts.material_evidence import (
    MaterialClaimBasis,
    MaterialEvidenceInsufficientCommand,
    MaterialEvidenceInsufficientReceipt,
    MaterialEvidenceKind,
    MaterialEvidenceReceipt,
    MaterialEvidenceRegistrationCommand,
    MaterialEvidenceRevocationCommand,
    MaterialEvidenceRevocationReceipt,
    MaterialEvidenceSnapshot,
    canonical_material_evidence_content_sha256,
    canonical_material_evidence_request_sha256,
    canonical_material_evidence_revocation_sha256,
)
from product_engine.models.classifications import (
    ProductClassificationAsset,
    ProductClassificationVersion,
)
from product_engine.models.material_evidence import (
    MaterialEvidenceRecord,
    MaterialEvidenceRevocationRecord,
    MaterialEvidenceUploadRecord,
)
from product_engine.models.products import Product
from product_engine.repositories.classifications import locked_product_statement


class MaterialEvidenceFailure(ValueError):
    pass


@dataclass(frozen=True)
class MaterialEvidenceActorContext:
    declared_by_ref: str
    reviewer_ref: str

    def __post_init__(self) -> None:
        if not self.declared_by_ref or not self.reviewer_ref:
            raise MaterialEvidenceFailure("material_evidence_actor_required")


def _classification_content_sha256(
    latest: ProductClassificationVersion,
    command: MaterialEvidenceRegistrationCommand,
    evidence_content_sha256: str,
) -> str:
    body = {
        "base_classification_sha256": latest.content_sha256,
        "material_label": command.material_label,
        "material_claim_basis": command.claim_basis,
        "material_evidence_content_sha256": evidence_content_sha256,
        "review_source": "material_evidence_registration",
    }
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _insufficient_classification_sha256(
    latest: ProductClassificationVersion,
    *,
    review_source: str,
    revocation_sha256: str | None,
) -> str:
    body = {
        "base_classification_sha256": latest.content_sha256,
        "material_label": "信息不足",
        "material_claim_basis": "information_insufficient",
        "review_source": review_source,
        "revocation_sha256": revocation_sha256,
    }
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class MaterialEvidenceRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    async def register(
        self,
        command: MaterialEvidenceRegistrationCommand,
        actor: MaterialEvidenceActorContext,
    ) -> MaterialEvidenceReceipt:
        product_id = await self._product_id(command.product_code)
        if product_id is None:
            raise MaterialEvidenceFailure("material_evidence_product_not_found")

        await self.session.execute(locked_product_statement(self.tenant_id, product_id))
        request_sha256 = canonical_material_evidence_request_sha256(command)
        content_sha256 = canonical_material_evidence_content_sha256(command)

        by_request = await self._evidence_by_request(command.request_id)
        if by_request is not None:
            if by_request.request_sha256 != request_sha256:
                raise MaterialEvidenceFailure("material_evidence_request_conflict")
            classification = await self._classification_by_evidence(by_request.id)
            return self._receipt(command.request_id, by_request, classification, "duplicate")

        latest = await self._latest_classification(product_id)
        if latest is None:
            raise MaterialEvidenceFailure("material_evidence_classification_not_found")
        if latest.id != command.base_classification_id:
            raise MaterialEvidenceFailure("stale_classification")

        by_content = await self._evidence_by_content(product_id, content_sha256)
        if by_content is not None:
            classification = await self._classification_by_evidence(by_content.id)
            return self._receipt(command.request_id, by_content, classification, "duplicate")

        staged_upload = await self._resolve_staged_upload(product_id, command)
        now = datetime.now(UTC)
        evidence = MaterialEvidenceRecord(
            id=uuid4(),
            tenant_id=self.tenant_id,
            product_id=product_id,
            base_classification_id=latest.id,
            material_label=command.material_label,
            claim_basis=command.claim_basis,
            evidence_kind=command.evidence_kind,
            evidence_ref=command.evidence_ref,
            evidence_sha256=command.evidence_sha256,
            source_summary=command.source_summary,
            declared_by_ref=actor.declared_by_ref,
            declared_at=now,
            reviewer_ref=actor.reviewer_ref,
            reviewed_at=now,
            request_id=command.request_id,
            request_sha256=request_sha256,
            content_sha256=content_sha256,
        )
        classification = self._new_classification(
            latest,
            command,
            actor,
            evidence.id,
            content_sha256,
            now,
        )
        existing_assets = await self._classification_assets(latest.id)
        copied_assets = [
            ProductClassificationAsset(
                id=uuid4(),
                tenant_id=self.tenant_id,
                classification_version_id=classification.id,
                source_image_id=asset.source_image_id,
                position=asset.position,
                role=asset.role,
                sha256=asset.sha256,
            )
            for asset in existing_assets
        ]

        try:
            async with self.session.begin_nested():
                self.session.add(evidence)
                await self.session.flush()
                self.session.add(classification)
                await self.session.flush()
                self.session.add_all(copied_assets)
                if staged_upload is not None:
                    staged_upload.status = "consumed"
                    staged_upload.consumed_at = now
                await self.session.flush()
        except IntegrityError as error:
            recovered = await self._recover_registration(
                command,
                product_id,
                request_sha256,
                content_sha256,
            )
            if recovered is not None:
                return recovered
            raise MaterialEvidenceFailure("material_evidence_ledger_conflict") from error

        return self._receipt(command.request_id, evidence, classification, "created")

    async def keep_insufficient(
        self,
        command: MaterialEvidenceInsufficientCommand,
    ) -> MaterialEvidenceInsufficientReceipt:
        product_id = await self._product_id(command.product_code)
        if product_id is None:
            raise MaterialEvidenceFailure("material_evidence_product_not_found")
        await self.session.execute(locked_product_statement(self.tenant_id, product_id))
        latest = await self._latest_classification(product_id)
        if latest is None:
            raise MaterialEvidenceFailure("material_evidence_classification_not_found")
        if latest.id != command.base_classification_id:
            raise MaterialEvidenceFailure("stale_classification")
        if latest.material_evidence_id is not None or latest.material_claim_basis in {
            "document_supported",
            "merchant_declared",
        }:
            raise MaterialEvidenceFailure("material_evidence_revocation_required")
        if (
            latest.material_claim_basis == "information_insufficient"
            or latest.material_evidence == "information_insufficient"
        ):
            return self._insufficient_receipt(
                command.request_id,
                command.product_code,
                latest,
                "duplicate",
            )

        now = datetime.now(UTC)
        classification = self._new_insufficient_classification(
            latest,
            request_id=command.request_id,
            reviewer_ref=latest.reviewer_ref,
            review_source="material_evidence_insufficient",
            revocation_sha256=None,
            now=now,
        )
        copied_assets = await self._copied_assets(latest.id, classification.id)
        async with self.session.begin_nested():
            self.session.add(classification)
            await self.session.flush()
            self.session.add_all(copied_assets)
            await self.session.flush()
        return self._insufficient_receipt(
            command.request_id,
            command.product_code,
            classification,
            "created",
        )

    async def revoke(
        self,
        command: MaterialEvidenceRevocationCommand,
        actor: MaterialEvidenceActorContext,
    ) -> MaterialEvidenceRevocationReceipt:
        request_sha256 = canonical_material_evidence_revocation_sha256(command)
        by_request = await self._revocation_by_request(command.request_id)
        if by_request is not None:
            if by_request.request_sha256 != request_sha256:
                raise MaterialEvidenceFailure("material_evidence_request_conflict")
            classification = await self._classification_by_request(command.request_id)
            return self._revocation_receipt(
                command.request_id,
                by_request.evidence_id,
                classification,
                "created",
            )

        evidence = await self._evidence_by_id(command.evidence_id)
        if evidence is None:
            raise MaterialEvidenceFailure("material_evidence_not_found")
        await self.session.execute(locked_product_statement(self.tenant_id, evidence.product_id))

        # Recheck after acquiring the product lock so concurrent revocations serialize.
        by_request = await self._revocation_by_request(command.request_id)
        if by_request is not None:
            if by_request.request_sha256 != request_sha256:
                raise MaterialEvidenceFailure("material_evidence_request_conflict")
            classification = await self._classification_by_request(command.request_id)
            return self._revocation_receipt(
                command.request_id,
                by_request.evidence_id,
                classification,
                "created",
            )
        existing_revocation = await self._revocation_by_evidence(evidence.id)
        if existing_revocation is not None:
            raise MaterialEvidenceFailure("material_evidence_invalid")

        latest = await self._latest_classification(evidence.product_id)
        if latest is None or latest.material_evidence_id != evidence.id:
            raise MaterialEvidenceFailure("material_evidence_invalid")

        now = datetime.now(UTC)
        revocation = MaterialEvidenceRevocationRecord(
            id=uuid4(),
            tenant_id=self.tenant_id,
            product_id=evidence.product_id,
            evidence_id=evidence.id,
            reason_code=command.reason_code,
            reason_summary=command.reason_summary,
            revoked_by_ref=actor.reviewer_ref,
            request_id=command.request_id,
            request_sha256=request_sha256,
        )
        classification = self._new_insufficient_classification(
            latest,
            request_id=command.request_id,
            reviewer_ref=actor.reviewer_ref,
            review_source="material_evidence_revocation",
            revocation_sha256=request_sha256,
            now=now,
        )
        copied_assets = await self._copied_assets(latest.id, classification.id)
        try:
            async with self.session.begin_nested():
                self.session.add(revocation)
                await self.session.flush()
                self.session.add(classification)
                await self.session.flush()
                self.session.add_all(copied_assets)
                await self.session.flush()
        except IntegrityError as error:
            recovered = await self._revocation_by_request(command.request_id)
            if recovered is not None:
                if recovered.request_sha256 != request_sha256:
                    raise MaterialEvidenceFailure("material_evidence_request_conflict") from error
                recovered_classification = await self._classification_by_request(command.request_id)
                return self._revocation_receipt(
                    command.request_id,
                    recovered.evidence_id,
                    recovered_classification,
                    "created",
                )
            raise MaterialEvidenceFailure("material_evidence_ledger_conflict") from error

        return self._revocation_receipt(
            command.request_id,
            evidence.id,
            classification,
            "created",
        )

    async def _product_id(self, product_code: str) -> UUID | None:
        return (
            await self.session.execute(
                select(Product.id).where(
                    Product.tenant_id == self.tenant_id,
                    Product.product_code == product_code,
                )
            )
        ).scalar_one_or_none()

    async def _latest_classification(
        self,
        product_id: UUID,
    ) -> ProductClassificationVersion | None:
        return (
            await self.session.execute(
                select(ProductClassificationVersion)
                .where(
                    ProductClassificationVersion.tenant_id == self.tenant_id,
                    ProductClassificationVersion.product_id == product_id,
                )
                .order_by(ProductClassificationVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _evidence_by_request(
        self,
        request_id: UUID,
    ) -> MaterialEvidenceRecord | None:
        return (
            await self.session.execute(
                select(MaterialEvidenceRecord).where(
                    MaterialEvidenceRecord.tenant_id == self.tenant_id,
                    MaterialEvidenceRecord.request_id == request_id,
                )
            )
        ).scalar_one_or_none()

    async def _evidence_by_content(
        self,
        product_id: UUID,
        content_sha256: str,
    ) -> MaterialEvidenceRecord | None:
        return (
            await self.session.execute(
                select(MaterialEvidenceRecord).where(
                    MaterialEvidenceRecord.tenant_id == self.tenant_id,
                    MaterialEvidenceRecord.product_id == product_id,
                    MaterialEvidenceRecord.content_sha256 == content_sha256,
                )
            )
        ).scalar_one_or_none()

    async def _evidence_by_id(
        self,
        evidence_id: UUID,
    ) -> MaterialEvidenceRecord | None:
        return (
            await self.session.execute(
                select(MaterialEvidenceRecord).where(
                    MaterialEvidenceRecord.tenant_id == self.tenant_id,
                    MaterialEvidenceRecord.id == evidence_id,
                )
            )
        ).scalar_one_or_none()

    async def _revocation_by_request(
        self,
        request_id: UUID,
    ) -> MaterialEvidenceRevocationRecord | None:
        return (
            await self.session.execute(
                select(MaterialEvidenceRevocationRecord).where(
                    MaterialEvidenceRevocationRecord.tenant_id == self.tenant_id,
                    MaterialEvidenceRevocationRecord.request_id == request_id,
                )
            )
        ).scalar_one_or_none()

    async def _revocation_by_evidence(
        self,
        evidence_id: UUID,
    ) -> MaterialEvidenceRevocationRecord | None:
        return (
            await self.session.execute(
                select(MaterialEvidenceRevocationRecord).where(
                    MaterialEvidenceRevocationRecord.tenant_id == self.tenant_id,
                    MaterialEvidenceRevocationRecord.evidence_id == evidence_id,
                )
            )
        ).scalar_one_or_none()

    async def _classification_by_request(
        self,
        request_id: UUID,
    ) -> ProductClassificationVersion:
        value = (
            await self.session.execute(
                select(ProductClassificationVersion).where(
                    ProductClassificationVersion.tenant_id == self.tenant_id,
                    ProductClassificationVersion.request_id == request_id,
                )
            )
        ).scalar_one_or_none()
        if value is None:
            raise MaterialEvidenceFailure("material_evidence_classification_not_found")
        return value

    async def _classification_by_evidence(
        self,
        evidence_id: UUID,
    ) -> ProductClassificationVersion:
        value = (
            await self.session.execute(
                select(ProductClassificationVersion).where(
                    ProductClassificationVersion.tenant_id == self.tenant_id,
                    ProductClassificationVersion.material_evidence_id == evidence_id,
                )
            )
        ).scalar_one_or_none()
        if value is None:
            raise MaterialEvidenceFailure("material_evidence_classification_not_found")
        return value

    async def _classification_assets(
        self,
        classification_id: UUID,
    ) -> tuple[ProductClassificationAsset, ...]:
        values = (
            await self.session.scalars(
                select(ProductClassificationAsset)
                .where(
                    ProductClassificationAsset.tenant_id == self.tenant_id,
                    ProductClassificationAsset.classification_version_id == classification_id,
                )
                .order_by(ProductClassificationAsset.position)
            )
        ).all()
        return tuple(values)

    async def _copied_assets(
        self,
        source_classification_id: UUID,
        target_classification_id: UUID,
    ) -> list[ProductClassificationAsset]:
        return [
            ProductClassificationAsset(
                id=uuid4(),
                tenant_id=self.tenant_id,
                classification_version_id=target_classification_id,
                source_image_id=asset.source_image_id,
                position=asset.position,
                role=asset.role,
                sha256=asset.sha256,
            )
            for asset in await self._classification_assets(source_classification_id)
        ]

    async def _resolve_staged_upload(
        self,
        product_id: UUID,
        command: MaterialEvidenceRegistrationCommand,
    ) -> MaterialEvidenceUploadRecord | None:
        if command.evidence_sha256 is None:
            return None
        upload = (
            await self.session.execute(
                select(MaterialEvidenceUploadRecord).where(
                    MaterialEvidenceUploadRecord.tenant_id == self.tenant_id,
                    MaterialEvidenceUploadRecord.product_id == product_id,
                    MaterialEvidenceUploadRecord.evidence_ref == command.evidence_ref,
                )
            )
        ).scalar_one_or_none()
        if upload is None or upload.status != "staged":
            raise MaterialEvidenceFailure("material_evidence_upload_not_staged")
        if upload.sha256 != command.evidence_sha256:
            raise MaterialEvidenceFailure("material_evidence_hash_mismatch")
        return upload

    @staticmethod
    def _new_classification(
        latest: ProductClassificationVersion,
        command: MaterialEvidenceRegistrationCommand,
        actor: MaterialEvidenceActorContext,
        evidence_id: UUID,
        evidence_content_sha256: str,
        now: datetime,
    ) -> ProductClassificationVersion:
        return ProductClassificationVersion(
            id=uuid4(),
            tenant_id=latest.tenant_id,
            product_id=latest.product_id,
            version=latest.version + 1,
            base_material_package_id=latest.base_material_package_id,
            base_material_package_version=latest.base_material_package_version,
            material_label=command.material_label,
            product_form=latest.product_form,
            main_asset_id=latest.main_asset_id,
            visual_observations=latest.visual_observations,
            selling_point_candidates=latest.selling_point_candidates,
            inventory_status=latest.inventory_status,
            price_status=latest.price_status,
            price_candidate=latest.price_candidate,
            service_rule_summaries=latest.service_rule_summaries,
            source_image_set_sha256=latest.source_image_set_sha256,
            review_status="human_confirmed",
            material_evidence="human_confirmed",
            material_claim_basis=command.claim_basis,
            material_evidence_id=evidence_id,
            review_source="material_evidence_registration",
            reviewer_ref=actor.reviewer_ref,
            reviewed_at=now,
            request_id=command.request_id,
            content_sha256=_classification_content_sha256(
                latest,
                command,
                evidence_content_sha256,
            ),
        )

    @staticmethod
    def _new_insufficient_classification(
        latest: ProductClassificationVersion,
        *,
        request_id: UUID,
        reviewer_ref: str,
        review_source: str,
        revocation_sha256: str | None,
        now: datetime,
    ) -> ProductClassificationVersion:
        return ProductClassificationVersion(
            id=uuid4(),
            tenant_id=latest.tenant_id,
            product_id=latest.product_id,
            version=latest.version + 1,
            base_material_package_id=latest.base_material_package_id,
            base_material_package_version=latest.base_material_package_version,
            material_label="信息不足",
            product_form=latest.product_form,
            main_asset_id=latest.main_asset_id,
            visual_observations=latest.visual_observations,
            selling_point_candidates=latest.selling_point_candidates,
            inventory_status=latest.inventory_status,
            price_status=latest.price_status,
            price_candidate=latest.price_candidate,
            service_rule_summaries=latest.service_rule_summaries,
            source_image_set_sha256=latest.source_image_set_sha256,
            review_status="human_confirmed",
            material_evidence="information_insufficient",
            material_claim_basis="information_insufficient",
            material_evidence_id=None,
            review_source=review_source,
            reviewer_ref=reviewer_ref,
            reviewed_at=now,
            request_id=request_id,
            content_sha256=_insufficient_classification_sha256(
                latest,
                review_source=review_source,
                revocation_sha256=revocation_sha256,
            ),
        )

    async def _recover_registration(
        self,
        command: MaterialEvidenceRegistrationCommand,
        product_id: UUID,
        request_sha256: str,
        content_sha256: str,
    ) -> MaterialEvidenceReceipt | None:
        by_request = await self._evidence_by_request(command.request_id)
        if by_request is not None:
            if by_request.request_sha256 != request_sha256:
                raise MaterialEvidenceFailure("material_evidence_request_conflict")
            classification = await self._classification_by_evidence(by_request.id)
            return self._receipt(command.request_id, by_request, classification, "duplicate")
        by_content = await self._evidence_by_content(product_id, content_sha256)
        if by_content is not None:
            classification = await self._classification_by_evidence(by_content.id)
            return self._receipt(command.request_id, by_content, classification, "duplicate")
        return None

    @staticmethod
    def _receipt(
        request_id: UUID,
        evidence: MaterialEvidenceRecord,
        classification: ProductClassificationVersion,
        disposition: Literal["created", "duplicate"],
    ) -> MaterialEvidenceReceipt:
        return MaterialEvidenceReceipt(
            request_id=request_id,
            evidence=MaterialEvidenceSnapshot(
                evidence_id=evidence.id,
                material_label=evidence.material_label,
                claim_basis=cast(MaterialClaimBasis, evidence.claim_basis),
                evidence_kind=cast(MaterialEvidenceKind, evidence.evidence_kind),
                evidence_content_sha256=evidence.content_sha256,
                revoked=False,
            ),
            classification_id=classification.id,
            classification_version=classification.version,
            disposition=disposition,
            downstream_status="not_sent",
        )

    @staticmethod
    def _insufficient_receipt(
        request_id: UUID,
        product_code: str,
        classification: ProductClassificationVersion,
        disposition: Literal["created", "duplicate"],
    ) -> MaterialEvidenceInsufficientReceipt:
        return MaterialEvidenceInsufficientReceipt(
            request_id=request_id,
            product_code=product_code,
            classification_id=classification.id,
            classification_version=classification.version,
            material_claim_basis="information_insufficient",
            disposition=disposition,
            downstream_status="not_sent",
        )

    @staticmethod
    def _revocation_receipt(
        request_id: UUID,
        evidence_id: UUID,
        classification: ProductClassificationVersion,
        disposition: Literal["created", "duplicate"],
    ) -> MaterialEvidenceRevocationReceipt:
        return MaterialEvidenceRevocationReceipt(
            request_id=request_id,
            evidence_id=evidence_id,
            classification_id=classification.id,
            classification_version=classification.version,
            material_claim_basis="information_insufficient",
            disposition=disposition,
            downstream_status="not_sent",
        )
