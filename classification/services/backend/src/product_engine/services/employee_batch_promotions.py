import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import ValidationError

from product_engine.contracts.album import ProductImageSetReadyV1, SourceImageRef
from product_engine.contracts.employee_formal_handoff import (
    EmployeeFormalHandoffReceipt,
    EmployeePromotionAsset,
    EmployeePromotionPlan,
    EmployeePromotionProduct,
    EmployeePromotionProductReceipt,
    canonical_employee_batch_sha256,
    employee_event_id,
    employee_source_task_id,
)
from product_engine.services.employee_handoff_receipts import EmployeeHandoffReceiptFailure
from product_engine.services.local_upload_batches import UploadBatch, UploadBatchItem
from product_engine.storage.protocol import StoredObject


class EmployeeBatchPromotionFailure(ValueError):
    pass


EmployeeImageMimeType = Literal["image/heic", "image/jpeg", "image/png"]


class EmployeeBatchStore(Protocol):
    def get(self, batch_id: str) -> UploadBatch | None: ...


class EmployeeTrainingVersion(Protocol):
    @property
    def change_source(self) -> str: ...

    @property
    def product_form(self) -> str: ...


class EmployeeTrainingVersionStore(Protocol):
    def current(self, template_code: str) -> EmployeeTrainingVersion | None: ...


class EmployeeSourceStorage(Protocol):
    async def read(self, object_key: str) -> bytes: ...


class EmployeeDestinationStorage(Protocol):
    async def write_verified(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        sha256: str,
    ) -> StoredObject: ...


class EmployeePromotionDatabaseWriter(Protocol):
    async def commit_batch(
        self,
        *,
        request_id: UUID,
        batch_id: str,
        content_sha256: str,
        events: tuple[ProductImageSetReadyV1, ...],
    ) -> Sequence[EmployeePromotionProductReceipt]: ...


class EmployeePromotionReceiptStore(Protocol):
    def latest(self, batch_id: str) -> EmployeeFormalHandoffReceipt | None: ...

    def resolve_replay(
        self,
        request_id: UUID,
        batch_id: str,
        content_sha256: str,
    ) -> EmployeeFormalHandoffReceipt | None: ...

    def save(self, receipt: EmployeeFormalHandoffReceipt) -> EmployeeFormalHandoffReceipt: ...


class EmployeePromotionProvenanceStore(Protocol):
    def save_employee_receipt(self, receipt: EmployeeFormalHandoffReceipt) -> None: ...


class EmployeeBatchPromotionService:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        batch_store: EmployeeBatchStore,
        training_version_store: EmployeeTrainingVersionStore,
        source_storage: EmployeeSourceStorage,
        destination_storage: EmployeeDestinationStorage,
        database_writer: EmployeePromotionDatabaseWriter,
        receipts: EmployeePromotionReceiptStore,
        provenance: EmployeePromotionProvenanceStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.batch_store = batch_store
        self.training_version_store = training_version_store
        self.source_storage = source_storage
        self.destination_storage = destination_storage
        self.database_writer = database_writer
        self.receipts = receipts
        self.provenance = provenance
        self.now = now or (lambda: datetime.now(UTC))

    def latest(self, batch_id: str) -> EmployeeFormalHandoffReceipt | None:
        try:
            return self.receipts.latest(batch_id)
        except EmployeeHandoffReceiptFailure as error:
            raise EmployeeBatchPromotionFailure(str(error)) from error

    async def promote(
        self,
        batch_id: str,
        request_id: UUID,
    ) -> EmployeeFormalHandoffReceipt:
        plan = self._build_plan(self._required_batch(batch_id))
        content_sha256 = canonical_employee_batch_sha256(plan)
        try:
            replay = self.receipts.resolve_replay(request_id, batch_id, content_sha256)
        except EmployeeHandoffReceiptFailure as error:
            raise EmployeeBatchPromotionFailure(str(error)) from error
        if replay is not None:
            return replay

        events = await self._prepare_objects_and_events(plan)
        products = tuple(
            await self.database_writer.commit_batch(
                request_id=request_id,
                batch_id=batch_id,
                content_sha256=content_sha256,
                events=events,
            )
        )
        try:
            receipt = EmployeeFormalHandoffReceipt.created_from(
                plan,
                request_id,
                products,
                created_at=self.now(),
            )
            self.provenance.save_employee_receipt(receipt)
            return self.receipts.save(receipt)
        except (EmployeeHandoffReceiptFailure, OSError, ValidationError, ValueError) as error:
            raise EmployeeBatchPromotionFailure("employee_handoff_ledger_conflict") from error

    def _required_batch(self, batch_id: str) -> UploadBatch:
        batch = self.batch_store.get(batch_id)
        if batch is None:
            raise EmployeeBatchPromotionFailure("employee_batch_not_found")
        if len(batch.items) != batch.expected_count or any(
            item.status != "reviewed" for item in batch.items
        ):
            raise EmployeeBatchPromotionFailure("employee_batch_review_incomplete")
        return batch

    def _build_plan(self, batch: UploadBatch) -> EmployeePromotionPlan:
        groups: dict[str, list[UploadBatchItem]] = {}
        for item in sorted(batch.items, key=lambda candidate: candidate.position):
            if item.product_code is None:
                raise EmployeeBatchPromotionFailure("employee_handoff_ledger_conflict")
            groups.setdefault(item.product_code, []).append(item)

        products: list[EmployeePromotionProduct] = []
        for product_code, items in groups.items():
            main_count = sum(item.image_role == "main" for item in items)
            if main_count != 1 or any(item.image_role not in {"main", "detail"} for item in items):
                raise EmployeeBatchPromotionFailure("employee_product_main_image_invalid")
            ordered_items = sorted(
                items,
                key=lambda item: (item.image_role != "main", item.position),
            )
            product_form = self._confirmed_form(ordered_items)
            assets: list[EmployeePromotionAsset] = []
            for position, item in enumerate(ordered_items):
                if (
                    item.object_key is None
                    or item.sha256 is None
                    or item.mime_type is None
                    or item.template_code is None
                ):
                    raise EmployeeBatchPromotionFailure("employee_source_image_unreadable")
                if item.mime_type not in {"image/heic", "image/jpeg", "image/png"}:
                    raise EmployeeBatchPromotionFailure("employee_source_image_unreadable")
                assets.append(
                    EmployeePromotionAsset(
                        item_id=item.item_id,
                        local_object_key=item.object_key,
                        destination_object_key=(
                            f"tenants/{self.tenant_id}/employee-handoffs/{batch.batch_id}/"
                            f"{product_code}/{item.item_id}"
                        ),
                        sha256=item.sha256,
                        mime_type=cast(EmployeeImageMimeType, item.mime_type),
                        role="main_candidate" if item.image_role == "main" else "gallery",
                        position=position,
                    )
                )
            try:
                products.append(
                    EmployeePromotionProduct(
                        product_code=product_code,
                        product_form=product_form,
                        assets=tuple(assets),
                    )
                )
            except ValidationError as error:
                code = (
                    "employee_product_main_image_invalid"
                    if "employee_product_main_image_invalid" in str(error)
                    else "employee_handoff_ledger_conflict"
                )
                raise EmployeeBatchPromotionFailure(code) from error
        try:
            return EmployeePromotionPlan(
                tenant_id=self.tenant_id,
                batch_id=batch.batch_id,
                products=tuple(products),
            )
        except ValidationError as error:
            raise EmployeeBatchPromotionFailure("employee_handoff_ledger_conflict") from error

    def _confirmed_form(self, items: list[UploadBatchItem]) -> str:
        forms: set[str] = set()
        for item in items:
            if item.template_code is None:
                raise EmployeeBatchPromotionFailure("employee_product_form_unconfirmed")
            version = self.training_version_store.current(item.template_code)
            if version is None:
                raise EmployeeBatchPromotionFailure("employee_product_form_unconfirmed")
            if version.change_source not in {
                "trainer_confirmed",
                "trainer_rollback",
            }:
                continue
            form = _canonical_form(version.product_form.strip())
            if not form:
                raise EmployeeBatchPromotionFailure("employee_product_form_unconfirmed")
            forms.add(form)
        if len(forms) != 1:
            raise EmployeeBatchPromotionFailure("employee_product_form_unconfirmed")
        return forms.pop()

    async def _prepare_objects_and_events(
        self,
        plan: EmployeePromotionPlan,
    ) -> tuple[ProductImageSetReadyV1, ...]:
        source_content: dict[UUID, bytes] = {}
        for product in plan.products:
            for asset in product.assets:
                try:
                    content = await self.source_storage.read(asset.local_object_key)
                except Exception as error:
                    raise EmployeeBatchPromotionFailure(
                        "employee_source_image_unreadable"
                    ) from error
                if hashlib.sha256(content).hexdigest() != asset.sha256:
                    raise EmployeeBatchPromotionFailure("employee_batch_already_changed")
                source_content[asset.item_id] = content

        events: list[ProductImageSetReadyV1] = []
        for product in plan.products:
            for asset in product.assets:
                try:
                    await self.destination_storage.write_verified(
                        asset.destination_object_key,
                        source_content[asset.item_id],
                        asset.mime_type,
                        asset.sha256,
                    )
                except Exception as error:
                    raise EmployeeBatchPromotionFailure(
                        "employee_handoff_storage_unavailable"
                    ) from error
            product_sha256 = _canonical_product_sha256(product)
            events.append(
                ProductImageSetReadyV1(
                    schema_version=1,
                    event_id=employee_event_id(
                        plan.tenant_id,
                        plan.batch_id,
                        product.product_code,
                        product_sha256,
                    ),
                    occurred_at=self.now(),
                    tenant_id=plan.tenant_id,
                    source_task_id=employee_source_task_id(plan.tenant_id, plan.batch_id),
                    product_code=product.product_code,
                    category="信息不足",
                    form=product.product_form,
                    classification_confidence=0.0,
                    images=[
                        SourceImageRef(
                            asset_id=asset.item_id,
                            object_key=asset.destination_object_key,
                            sha256=asset.sha256,
                            mime_type=asset.mime_type,
                            kind="original",
                            role=asset.role,
                            quality_confidence=0.0,
                        )
                        for asset in product.assets
                    ],
                )
            )
        return tuple(events)


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


def _canonical_product_sha256(product: EmployeePromotionProduct) -> str:
    canonical = json.dumps(
        product.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
