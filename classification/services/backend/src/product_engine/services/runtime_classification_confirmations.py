from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    ClassificationConfirmationReceipt,
)
from product_engine.db import Database
from product_engine.repositories.classifications import (
    ClassificationLedgerFailure,
    ClassificationLedgerRepository,
)
from product_engine.services.classification_confirmations import (
    ClassificationConfirmationFailure,
    ClassificationConfirmationService,
    LocalClassificationReview,
    LocalClassificationReviewItem,
)
from product_engine.services.classification_import_provenance import (
    ClassificationImportProvenance,
    ClassificationImportProvenanceFailure,
    ClassificationImportProvenanceStore,
)
from product_engine.services.local_upload_batches import (
    LocalUploadBatchStore,
    UploadBatchItem,
)
from product_engine.services.training_label_versions import (
    InventoryStatus,
    TrainingLabelVersion,
    TrainingLabelVersionStore,
)

ClassificationAssetRole = Literal["main_candidate", "gallery", "source_original"]


class ClassificationConfirmationWriter(Protocol):
    async def confirm(
        self,
        *,
        product_code: str,
        package_version: int,
        request_id: UUID,
        local_batch_id: str,
    ) -> ClassificationConfirmationReceipt: ...


class RuntimeClassificationConfirmationWriter:
    def __init__(
        self,
        *,
        database: Database,
        tenant_id: UUID,
        batch_store: LocalUploadBatchStore,
        training_version_store: TrainingLabelVersionStore,
        album_review_imports_path: Path,
        reviewer_ref: str = "local-operator",
    ) -> None:
        self.database = database
        self.tenant_id = tenant_id
        self.batch_store = batch_store
        self.training_version_store = training_version_store
        self.album_review_imports_path = album_review_imports_path
        self.provenance_store = ClassificationImportProvenanceStore(album_review_imports_path)
        self.reviewer_ref = reviewer_ref

    async def confirm(
        self,
        *,
        product_code: str,
        package_version: int,
        request_id: UUID,
        local_batch_id: str,
    ) -> ClassificationConfirmationReceipt:
        command, local_review = self._build_command(
            product_code=product_code,
            package_version=package_version,
            request_id=request_id,
            local_batch_id=local_batch_id,
        )
        async with self.database.session() as session:
            repository = ClassificationLedgerRepository(session, self.tenant_id)
            service = ClassificationConfirmationService(
                repository=repository,
                reviewer_ref=self.reviewer_ref,
                local_review_loader=lambda _batch_id: local_review,
            )
            try:
                return await service.confirm(command)
            except ClassificationLedgerFailure as error:
                raise ClassificationConfirmationFailure(str(error)) from error

    def _build_command(
        self,
        *,
        product_code: str,
        package_version: int,
        request_id: UUID,
        local_batch_id: str,
    ) -> tuple[ClassificationConfirmationCommand, LocalClassificationReview]:
        batch = self.batch_store.get(local_batch_id)
        if batch is None:
            raise ClassificationConfirmationFailure("product_review_identity_mismatch")
        items = tuple(
            sorted(
                (item for item in batch.items if item.product_code == product_code),
                key=lambda item: item.position,
            )
        )
        if not items:
            raise ClassificationConfirmationFailure("product_review_identity_mismatch")
        if any(item.status != "reviewed" for item in items):
            raise ClassificationConfirmationFailure("product_review_incomplete")

        provenance, imported_assets = self._load_provenance(
            product_code, package_version, local_batch_id, items
        )
        versions = tuple(self._training_version(item) for item in items)
        confirmed_versions = tuple(
            version
            for version in versions
            if version.change_source in {"trainer_confirmed", "trainer_rollback"}
        )
        if not confirmed_versions:
            raise ClassificationConfirmationFailure("product_review_incomplete")
        if provenance.source_type == "employee_import_promotion":
            material_label = "信息不足"
            product_form = self._single_confirmed_form(confirmed_versions)
            selling_point_candidates: list[str] = []
            inventory_status = None
            service_rule_summaries: list[str] = []
        else:
            material_label = self._single_confirmed_material(confirmed_versions)
            product_form = self._single_confirmed_form(confirmed_versions)
            selling_point_candidates = self._unique_lines(
                version.selling_points for version in versions
            )
            inventory_status = self._agreed_inventory_status(versions)
            service_rule_summaries = self._unique_lines(
                (rule.customer_summary for rule in version.service_rules) for version in versions
            )

        local_review = LocalClassificationReview(
            product_code=product_code,
            items=tuple(
                LocalClassificationReviewItem(
                    asset_id=imported_assets[item.item_id][0],
                    status=item.status,
                    material_label=material_label,
                    product_form=product_form,
                )
                for item in items
            ),
        )
        assets = [
            ClassificationAssetConfirmation(
                asset_id=imported_assets[item.item_id][0],
                position=position,
                role=imported_assets[item.item_id][1],
                sha256=self._required_sha256(item),
            )
            for position, item in enumerate(items)
        ]
        main_assets = [asset.asset_id for asset in assets if asset.role == "main_candidate"]
        if len(main_assets) != 1:
            raise ClassificationConfirmationFailure("classification_asset_mismatch")

        command = ClassificationConfirmationCommand(
            request_id=request_id,
            product_code=product_code,
            local_batch_id=local_batch_id,
            base_material_package_version=package_version,
            material_label=material_label,
            product_form=product_form,
            main_asset_id=main_assets[0],
            visual_observations=self._unique_lines(
                version.visual_observations for version in versions
            ),
            selling_point_candidates=selling_point_candidates,
            inventory_status=inventory_status,
            price_status="pending",
            price_candidate=None,
            service_rule_summaries=service_rule_summaries,
            review_source="local_trainer_review",
            assets=assets,
        )
        return command, local_review

    def _load_provenance(
        self,
        product_code: str,
        package_version: int,
        local_batch_id: str,
        items: tuple[UploadBatchItem, ...],
    ) -> tuple[
        ClassificationImportProvenance,
        dict[UUID, tuple[UUID, ClassificationAssetRole]],
    ]:
        try:
            record = self.provenance_store.get(product_code, package_version)
        except ClassificationImportProvenanceFailure as error:
            raise ClassificationConfirmationFailure(
                "classification_import_provenance_missing"
            ) from error
        if record is None or record.batch_id != local_batch_id or not record.assets:
            raise ClassificationConfirmationFailure("classification_import_provenance_missing")
        result = {asset.item_id: (asset.asset_id, asset.role) for asset in record.assets}
        if set(result) != {item.item_id for item in items}:
            raise ClassificationConfirmationFailure("classification_import_provenance_missing")
        return record, result

    @staticmethod
    def _single_confirmed_material(
        versions: tuple[TrainingLabelVersion, ...],
    ) -> str:
        materials = {version.material_label for version in versions}
        if len(materials) != 1:
            raise ClassificationConfirmationFailure("product_review_value_mismatch")
        return next(iter(materials))

    @classmethod
    def _single_confirmed_form(
        cls,
        versions: tuple[TrainingLabelVersion, ...],
    ) -> str:
        forms = {cls._canonical_form(version.product_form) for version in versions}
        if len(forms) != 1:
            raise ClassificationConfirmationFailure("product_review_value_mismatch")
        return next(iter(forms))

    def _training_version(self, item: UploadBatchItem) -> TrainingLabelVersion:
        if item.template_code is None:
            raise ClassificationConfirmationFailure("product_review_incomplete")
        version = self.training_version_store.current(item.template_code)
        if version is None:
            raise ClassificationConfirmationFailure("product_review_incomplete")
        return version

    @staticmethod
    def _canonical_form(value: str) -> str:
        return {
            "未知": "unknown",
            "手镯": "bangle",
            "单珠": "bead",
            "手串": "bracelet",
            "胸针": "brooch",
            "耳饰": "earring",
            "项链": "necklace",
            "吊坠": "pendant",
            "戒指": "ring",
            "串珠": "strand",
            "方圆珠手串": "round_bead_bracelet",
        }.get(value, value)

    @staticmethod
    def _required_sha256(item: UploadBatchItem) -> str:
        if item.sha256 is None:
            raise ClassificationConfirmationFailure("classification_asset_mismatch")
        return item.sha256

    @staticmethod
    def _unique_lines(groups: Iterable[Iterable[str]]) -> list[str]:
        return list(dict.fromkeys(line for group in groups for line in group if line.strip()))

    @staticmethod
    def _agreed_inventory_status(
        versions: tuple[TrainingLabelVersion, ...],
    ) -> InventoryStatus | None:
        values = {version.inventory_status for version in versions}
        return values.pop() if len(values) == 1 else None
