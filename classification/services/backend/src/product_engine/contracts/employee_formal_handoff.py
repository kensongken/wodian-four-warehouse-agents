import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.contracts.common import ProductCode, Sha256Hex

EMPLOYEE_HANDOFF_NAMESPACE = UUID("6ce1d7c7-4006-4cf4-8c0a-763251c07c7c")


class EmployeeFormalHandoffRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID


class EmployeePromotionAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: UUID
    local_object_key: str = Field(min_length=1, max_length=1024)
    destination_object_key: str = Field(min_length=1, max_length=1024)
    sha256: Sha256Hex
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    role: Literal["main_candidate", "gallery"]
    position: int = Field(ge=0)


class EmployeePromotionProduct(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    product_code: ProductCode
    product_form: str = Field(min_length=1, max_length=64)
    assets: tuple[EmployeePromotionAsset, ...] = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def validate_assets(self) -> Self:
        positions = [asset.position for asset in self.assets]
        if positions != list(range(len(self.assets))):
            raise ValueError("employee_asset_positions_invalid")
        item_ids = [asset.item_id for asset in self.assets]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("employee_handoff_ledger_conflict")
        destination_keys = [asset.destination_object_key for asset in self.assets]
        if len(destination_keys) != len(set(destination_keys)):
            raise ValueError("employee_handoff_ledger_conflict")
        if sum(asset.role == "main_candidate" for asset in self.assets) != 1:
            raise ValueError("employee_product_main_image_invalid")
        return self


class EmployeePromotionPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    batch_id: str = Field(min_length=1, max_length=128)
    products: tuple[EmployeePromotionProduct, ...] = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def validate_products(self) -> Self:
        product_codes = [product.product_code for product in self.products]
        if len(product_codes) != len(set(product_codes)):
            raise ValueError("employee_handoff_ledger_conflict")
        item_ids = [asset.item_id for product in self.products for asset in product.assets]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("employee_handoff_ledger_conflict")
        destination_keys = [
            asset.destination_object_key for product in self.products for asset in product.assets
        ]
        if len(destination_keys) != len(set(destination_keys)):
            raise ValueError("employee_handoff_ledger_conflict")
        return self


class EmployeePromotionAssetReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: UUID
    asset_id: UUID
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: Sha256Hex
    mime_type: Literal["image/heic", "image/jpeg", "image/png"]
    role: Literal["main_candidate", "gallery", "source_original"]
    position: int = Field(ge=0)


class EmployeePromotionProductReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    product_code: ProductCode
    package_version: int = Field(ge=1)
    event_id: UUID
    asset_count: int = Field(ge=1)
    content_sha256: Sha256Hex
    assets: tuple[EmployeePromotionAssetReceipt, ...] = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def validate_assets(self) -> Self:
        if self.asset_count != len(self.assets):
            raise ValueError("employee_handoff_ledger_conflict")
        if [asset.position for asset in self.assets] != list(range(len(self.assets))):
            raise ValueError("employee_handoff_ledger_conflict")
        asset_ids = [asset.asset_id for asset in self.assets]
        item_ids = [asset.item_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)) or len(item_ids) != len(set(item_ids)):
            raise ValueError("employee_handoff_ledger_conflict")
        return self

    @classmethod
    def from_event(
        cls,
        event: ProductImageSetReadyV1,
        package_version: int,
    ) -> "EmployeePromotionProductReceipt":
        return cls(
            product_code=event.product_code,
            package_version=package_version,
            event_id=event.event_id,
            asset_count=len(event.images),
            content_sha256=_canonical_product_image_set_sha256(event),
            assets=tuple(
                EmployeePromotionAssetReceipt(
                    item_id=asset.asset_id,
                    asset_id=asset.asset_id,
                    object_key=asset.object_key,
                    sha256=asset.sha256,
                    mime_type=asset.mime_type,
                    role=asset.role,
                    position=position,
                )
                for position, asset in enumerate(event.images)
            ),
        )


class EmployeeFormalHandoffReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    tenant_id: UUID
    batch_id: str = Field(min_length=1, max_length=128)
    content_sha256: Sha256Hex
    products: tuple[EmployeePromotionProductReceipt, ...] = Field(min_length=1, max_length=3000)
    source_type: Literal["employee_import_promotion"] = "employee_import_promotion"
    downstream_status: Literal["not_sent"] = "not_sent"
    created_at: datetime
    disposition: Literal["created", "duplicate"]

    @model_validator(mode="after")
    def validate_products(self) -> Self:
        product_codes = [product.product_code for product in self.products]
        event_ids = [product.event_id for product in self.products]
        if len(product_codes) != len(set(product_codes)) or len(event_ids) != len(set(event_ids)):
            raise ValueError("employee_handoff_ledger_conflict")
        return self

    @classmethod
    def created_from(
        cls,
        plan: EmployeePromotionPlan,
        request_id: UUID,
        products: tuple[EmployeePromotionProductReceipt, ...],
        *,
        created_at: datetime | None = None,
    ) -> "EmployeeFormalHandoffReceipt":
        if {product.product_code for product in products} != {
            product.product_code for product in plan.products
        }:
            raise ValueError("employee_handoff_ledger_conflict")
        return cls(
            request_id=request_id,
            tenant_id=plan.tenant_id,
            batch_id=plan.batch_id,
            content_sha256=canonical_employee_batch_sha256(plan),
            products=products,
            created_at=created_at or datetime.now(UTC),
            disposition="created",
        )


def employee_source_task_id(tenant_id: UUID, batch_id: str) -> UUID:
    return uuid5(EMPLOYEE_HANDOFF_NAMESPACE, f"source:{tenant_id}:{batch_id}")


def employee_event_id(
    tenant_id: UUID,
    batch_id: str,
    product_code: str,
    content_sha256: str,
) -> UUID:
    return uuid5(
        EMPLOYEE_HANDOFF_NAMESPACE,
        f"event:{tenant_id}:{batch_id}:{product_code}:{content_sha256}",
    )


def canonical_employee_batch_sha256(plan: EmployeePromotionPlan) -> str:
    canonical = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _canonical_product_image_set_sha256(event: ProductImageSetReadyV1) -> str:
    canonical = json.dumps(
        event.model_dump(mode="json", exclude={"event_id", "occurred_at"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
