import json
from pathlib import Path
from threading import RLock
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from product_engine.contracts.common import Sha256Hex
from product_engine.contracts.employee_formal_handoff import EmployeeFormalHandoffReceipt
from product_engine.services.local_atomic_json import atomic_write_json


class ClassificationImportProvenanceFailure(ValueError):
    pass


class ClassificationImportAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: UUID
    asset_id: UUID
    position: int = Field(ge=0)
    role: Literal["main_candidate", "gallery", "source_original"]
    sha256: Sha256Hex | None = None


class ClassificationImportProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: Literal["album_review_import", "employee_import_promotion"] = "album_review_import"
    batch_id: str = Field(min_length=1, max_length=128)
    assets: tuple[ClassificationImportAsset, ...] = ()

    @model_validator(mode="after")
    def validate_assets(self) -> Self:
        if self.assets and [asset.position for asset in self.assets] != list(
            range(len(self.assets))
        ):
            raise ValueError("classification_import_provenance_invalid")
        item_ids = [asset.item_id for asset in self.assets]
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(item_ids) != len(set(item_ids)) or len(asset_ids) != len(set(asset_ids)):
            raise ValueError("classification_import_provenance_invalid")
        if self.source_type == "employee_import_promotion" and (
            not self.assets or any(asset.sha256 is None for asset in self.assets)
        ):
            raise ValueError("classification_import_provenance_invalid")
        return self


_CLASSIFICATION_IMPORT_PROVENANCE_LOCK = RLock()


class ClassificationImportProvenanceStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> tuple[dict[str, object], dict[str, ClassificationImportProvenance]]:
        if not self.path.exists():
            return {}, {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("classification_import_provenance_invalid")
            records: dict[str, ClassificationImportProvenance] = {}
            for key, value in raw.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("classification_import_provenance_invalid")
                if isinstance(value, str):
                    records[key] = ClassificationImportProvenance(batch_id=value)
                elif isinstance(value, dict):
                    records[key] = ClassificationImportProvenance.model_validate(value)
                else:
                    raise ValueError("classification_import_provenance_invalid")
            return raw, records
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise ClassificationImportProvenanceFailure(
                "classification_import_provenance_invalid"
            ) from error

    def get(
        self,
        product_code: str,
        package_version: int,
    ) -> ClassificationImportProvenance | None:
        with _CLASSIFICATION_IMPORT_PROVENANCE_LOCK:
            _, records = self._read()
            return records.get(f"{product_code}:v{package_version}")

    def save(
        self,
        product_code: str,
        package_version: int,
        record: ClassificationImportProvenance,
    ) -> None:
        with _CLASSIFICATION_IMPORT_PROVENANCE_LOCK:
            raw, records = self._read()
            key = f"{product_code}:v{package_version}"
            existing = records.get(key)
            if existing is not None and existing != record:
                raise ClassificationImportProvenanceFailure(
                    "classification_import_provenance_conflict"
                )
            raw[key] = record.model_dump(mode="json")
            atomic_write_json(self.path, raw)

    def save_employee_receipt(self, receipt: EmployeeFormalHandoffReceipt) -> None:
        with _CLASSIFICATION_IMPORT_PROVENANCE_LOCK:
            raw, records = self._read()
            for product in receipt.products:
                key = f"{product.product_code}:v{product.package_version}"
                record = ClassificationImportProvenance(
                    source_type="employee_import_promotion",
                    batch_id=receipt.batch_id,
                    assets=tuple(
                        ClassificationImportAsset(
                            item_id=asset.item_id,
                            asset_id=asset.asset_id,
                            position=asset.position,
                            role=asset.role,
                            sha256=asset.sha256,
                        )
                        for asset in product.assets
                    ),
                )
                existing = records.get(key)
                if existing is not None and existing != record:
                    raise ClassificationImportProvenanceFailure(
                        "classification_import_provenance_conflict"
                    )
                raw[key] = record.model_dump(mode="json")
            atomic_write_json(self.path, raw)
