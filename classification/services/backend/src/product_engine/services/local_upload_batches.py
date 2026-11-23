import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator

from product_engine.contracts.style_test_bridge import PhotoCategory
from product_engine.services.photo_usage_classifier import (
    LOCAL_CLASSIFIER_VERSION,
    PhotoUsageCorrectionEvent,
    PhotoUsageDecision,
    record_photo_usage_correction,
)
from product_engine.services.template_registry import TemplateRecord


class LocalUploadBatchFailure(ValueError):
    pass


_UPLOAD_BATCH_LOCK = RLock()

StyleTestConfirmationVersionReason = Literal[
    "product_binding",
    "classification_correction",
    "cross_batch_move",
    "payload_mutation",
]


LocalProductFormCode = Literal[
    "unknown",
    "bangle",
    "bead",
    "bracelet",
    "brooch",
    "earring",
    "necklace",
    "pendant",
    "ring",
    "strand",
    "round_bead_bracelet",
    "BraceletModel2026",
    "手镯",
    "单珠",
    "手串",
    "胸针",
    "耳饰",
    "项链",
    "吊坠",
    "戒指",
    "串珠",
    "方圆珠手串",
]


class InitialRecognitionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    material_label: str = "待确认"
    product_form: LocalProductFormCode = "unknown"
    visual_observations: list[str] = ["待训练员确认图片可见特征"]
    selling_points: list[str] = ["待训练员补充可用销售卖点"]
    price_status: Literal["pending_standard"] = "pending_standard"
    price_reason: str = "待录入该品类的价格标准"
    training_state: Literal["system_initial_model_not_trained"] = "system_initial_model_not_trained"
    authenticity_verification: Literal["not_verified"] = "not_verified"
    origin_verification: Literal["not_verified"] = "not_verified"
    quality_verification: Literal["not_verified"] = "not_verified"
    warehouse_verification: Literal["pending_warehouse_verification"] = (
        "pending_warehouse_verification"
    )

    @field_validator("product_form", mode="before")
    @classmethod
    def migrate_legacy_pending_product_form(cls, value: object) -> object:
        if value in {"待确认", "未知"}:
            return "unknown"
        return value


class PhotoClassificationConfirmationFact(BaseModel):
    """One append-only, reviewable classification fact for a stored upload item."""

    model_config = ConfigDict(frozen=True)

    automatic_decision: PhotoUsageDecision
    human_correction: PhotoUsageCorrectionEvent | None = None
    final_category: PhotoCategory
    classifier_version: Literal["local-photo-usage-rules-v1"] = LOCAL_CLASSIFIER_VERSION
    confirmation_version: StrictInt

    @model_validator(mode="after")
    def retain_automatic_decision_and_validate_final_category(
        self,
    ) -> "PhotoClassificationConfirmationFact":
        if type(self.confirmation_version) is not int or self.confirmation_version < 1:
            raise ValueError("invalid_confirmation_version")
        if self.human_correction is None:
            if self.final_category != self.automatic_decision.category:
                raise ValueError("final_category_requires_human_correction")
        elif (
            self.human_correction.automatic_decision != self.automatic_decision
            or self.final_category != self.human_correction.corrected_category
        ):
            raise ValueError("human_correction_mismatch")
        return self


class StyleTestPermissionFact(BaseModel):
    """Explicit, batch-scoped permission for the local desensitized test only."""

    model_config = ConfigDict(frozen=True)

    customer_test_id: str
    product_id: str
    confirmation_version: StrictInt
    transform_permission: bool
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_explicit_permission(self) -> "StyleTestPermissionFact":
        if not self.customer_test_id.strip() or not self.product_id.strip():
            raise ValueError("style_test_identity_required")
        if type(self.confirmation_version) is not int or self.confirmation_version < 1:
            raise ValueError("invalid_confirmation_version")
        return self


class StyleTestProductDraftBindingFact(BaseModel):
    """An immutable product identity bound before a batch is submitted."""

    model_config = ConfigDict(frozen=True)

    customer_test_id: str
    product_id: str
    product_category: str
    product_form: str
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_binding(self) -> "StyleTestProductDraftBindingFact":
        if not all(
            value.strip()
            for value in (
                self.customer_test_id,
                self.product_id,
                self.product_category,
                self.product_form,
            )
        ):
            raise ValueError("style_test_product_binding_invalid")
        return self


class StyleTestConfirmationVersionFact(BaseModel):
    """An append-only product version reservation owned by one batch."""

    model_config = ConfigDict(frozen=True)

    product_id: str
    confirmation_version: StrictInt
    reason: StyleTestConfirmationVersionReason
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_reservation(self) -> "StyleTestConfirmationVersionFact":
        if (
            not self.product_id.strip()
            or type(self.confirmation_version) is not int
            or self.confirmation_version < 1
        ):
            raise ValueError("invalid_confirmation_version")
        return self


class StyleTestProductConfirmationFact(BaseModel):
    """A batch binding to one startup-allowlisted, desensitized test product."""

    model_config = ConfigDict(frozen=True)

    customer_test_id: str
    product_id: str
    product_category: str
    product_form: str
    confirmation_version: StrictInt
    main_photo_id: UUID
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_confirmation(self) -> "StyleTestProductConfirmationFact":
        if (
            not all(
                value.strip()
                for value in (
                    self.customer_test_id,
                    self.product_id,
                    self.product_category,
                    self.product_form,
                )
            )
            or type(self.confirmation_version) is not int
            or self.confirmation_version < 1
        ):
            raise ValueError("style_test_product_confirmation_invalid")
        return self


class UploadBatchItemAudit(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["removed", "moved", "replaced"]
    item_id: UUID
    from_position: int
    to_position: int | None = None
    recorded_at: datetime


class UploadBatchItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: UUID
    position: int
    filename: str
    status: Literal["pending_review", "reviewed", "duplicate", "failed"]
    recognition_status: Literal[
        "pending", "succeeded", "fallback_exact_match", "unavailable", "failed"
    ] = "pending"
    template_code: str | None
    sha256: str | None
    mime_type: str | None
    object_key: str | None
    product_code: str | None = None
    product_sequence: int | None = None
    image_role: Literal["main", "detail"] | None = None
    product_name: str | None = None
    merchant_price_reference: str | None = None
    source_system: Literal["local_upload", "employee_workbook", "album_organizer"] = "local_upload"
    duplicate_of_item_id: UUID | None = None
    error_code: str | None = None
    initial_result: InitialRecognitionResult = InitialRecognitionResult()
    photo_classification_facts: tuple[PhotoClassificationConfirmationFact, ...] = ()
    created_at: datetime


class UploadBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str
    expected_count: int
    items: list[UploadBatchItem]
    created_at: datetime
    updated_at: datetime
    style_test_permission: StyleTestPermissionFact | None = None
    style_test_product_draft_binding: StyleTestProductDraftBindingFact | None = None
    style_test_product_confirmation: StyleTestProductConfirmationFact | None = None
    style_test_confirmation_versions: tuple[StyleTestConfirmationVersionFact, ...] = ()
    item_audit: tuple[UploadBatchItemAudit, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "expected": self.expected_count,
            "uploaded": len(self.items),
            "success": sum(item.status in {"pending_review", "reviewed"} for item in self.items),
            "duplicate": sum(item.status == "duplicate" for item in self.items),
            "failed": sum(item.status == "failed" for item in self.items),
            "pending_review": sum(item.status == "pending_review" for item in self.items),
            "reviewed": sum(item.status == "reviewed" for item in self.items),
        }


def _recompute_duplicate_relationships(
    items: list[UploadBatchItem],
) -> list[UploadBatchItem]:
    """Rebuild each live hash chain so every duplicate points inside its batch."""

    rebuilt: dict[UUID, UploadBatchItem] = {}
    groups: dict[str, list[UploadBatchItem]] = {}
    for item in items:
        if item.status == "failed" or not item.sha256:
            rebuilt[item.item_id] = item.model_copy(update={"duplicate_of_item_id": None})
            continue
        groups.setdefault(item.sha256, []).append(item)
    for group in groups.values():
        ordered = sorted(group, key=lambda item: (item.position, str(item.item_id)))
        primary = ordered[0]
        rebuilt[primary.item_id] = primary.model_copy(
            update={
                "status": ("pending_review" if primary.status == "duplicate" else primary.status),
                "duplicate_of_item_id": None,
            }
        )
        for duplicate in ordered[1:]:
            rebuilt[duplicate.item_id] = duplicate.model_copy(
                update={
                    "status": "duplicate",
                    "duplicate_of_item_id": primary.item_id,
                }
            )
    return [rebuilt[item.item_id] for item in items]


def _batch_confirmation_version(batch: UploadBatch) -> int | None:
    versions = [
        fact.confirmation_version
        for item in batch.items
        for fact in item.photo_classification_facts
    ]
    versions.extend(fact.confirmation_version for fact in batch.style_test_confirmation_versions)
    if batch.style_test_permission is not None:
        versions.append(batch.style_test_permission.confirmation_version)
    if batch.style_test_product_confirmation is not None:
        versions.append(batch.style_test_product_confirmation.confirmation_version)
    return max(versions) if versions else None


def _active_item_confirmation_versions(batch: UploadBatch) -> tuple[int, ...]:
    versions: list[int] = []
    for item in batch.items:
        if item.status not in {"pending_review", "reviewed"}:
            continue
        if not item.photo_classification_facts:
            raise LocalUploadBatchFailure("photo_classification_required")
        versions.append(item.photo_classification_facts[-1].confirmation_version)
    return tuple(versions)


def _product_confirmation_version_max(
    batches: list[UploadBatch],
    product_id: str,
) -> int:
    versions: list[int] = []
    for batch in batches:
        versions.extend(
            fact.confirmation_version
            for fact in batch.style_test_confirmation_versions
            if fact.product_id == product_id
        )
        if (
            batch.style_test_product_confirmation is not None
            and batch.style_test_product_confirmation.product_id == product_id
        ):
            versions.append(batch.style_test_product_confirmation.confirmation_version)
        if (
            batch.style_test_permission is not None
            and batch.style_test_permission.product_id == product_id
        ):
            versions.append(batch.style_test_permission.confirmation_version)
    return max(versions, default=0)


def _carry_item_facts_to_version(
    batch: UploadBatch,
    confirmation_version: int,
) -> UploadBatch:
    """Carry every stored classification lineage without changing item status."""

    updated_items: list[UploadBatchItem] = []
    for item in batch.items:
        if not item.photo_classification_facts:
            if item.status in {"pending_review", "reviewed"}:
                raise LocalUploadBatchFailure("photo_classification_required")
            updated_items.append(item)
            continue
        latest = item.photo_classification_facts[-1]
        if latest.confirmation_version == confirmation_version:
            updated_items.append(item)
            continue
        if latest.confirmation_version > confirmation_version:
            raise LocalUploadBatchFailure("confirmation_version_conflict")
        carried = PhotoClassificationConfirmationFact(
            automatic_decision=latest.automatic_decision,
            human_correction=latest.human_correction,
            final_category=latest.final_category,
            classifier_version=latest.classifier_version,
            confirmation_version=confirmation_version,
        )
        updated_items.append(
            item.model_copy(
                update={
                    "photo_classification_facts": tuple(
                        sorted(
                            (*item.photo_classification_facts, carried),
                            key=lambda fact: fact.confirmation_version,
                        )
                    )
                }
            )
        )
    return batch.model_copy(update={"items": updated_items})


def _record_bound_product_version(
    batch: UploadBatch,
    *,
    product_id: str,
    confirmation_version: int,
    reason: StyleTestConfirmationVersionReason,
) -> UploadBatch:
    """Append one reservation and align or safely invalidate final product facts."""

    reservations = batch.style_test_confirmation_versions
    if not any(
        fact.product_id == product_id and fact.confirmation_version == confirmation_version
        for fact in reservations
    ):
        reservations = (
            *reservations,
            StyleTestConfirmationVersionFact(
                product_id=product_id,
                confirmation_version=confirmation_version,
                reason=reason,
                recorded_at=datetime.now(UTC),
            ),
        )
    product = batch.style_test_product_confirmation
    permission = batch.style_test_permission
    if product is not None:
        active_item_ids = {
            item.item_id for item in batch.items if item.status in {"pending_review", "reviewed"}
        }
        if product.main_photo_id not in active_item_ids:
            # The product identity remains durably draft-bound, but a deleted or
            # moved main image makes the final confirmation unsafe to reuse.
            product = None
            permission = None
        else:
            product = StyleTestProductConfirmationFact(
                customer_test_id=product.customer_test_id,
                product_id=product.product_id,
                product_category=product.product_category,
                product_form=product.product_form,
                confirmation_version=confirmation_version,
                main_photo_id=product.main_photo_id,
                recorded_at=datetime.now(UTC),
            )
    if permission is not None:
        permission = StyleTestPermissionFact(
            customer_test_id=permission.customer_test_id,
            product_id=permission.product_id,
            confirmation_version=confirmation_version,
            transform_permission=permission.transform_permission,
            recorded_at=datetime.now(UTC),
        )
    return batch.model_copy(
        update={
            "style_test_confirmation_versions": reservations,
            "style_test_permission": permission,
            "style_test_product_confirmation": product,
            "updated_at": datetime.now(UTC),
        }
    )


class LocalUploadBatchStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> list[UploadBatch]:
        if not self.path.exists():
            return []
        raw: list[dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
        return [UploadBatch.model_validate(item) for item in raw]

    def _write(self, batches: list[UploadBatch]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.writing")
        temporary.write_text(
            json.dumps(
                [batch.model_dump(mode="json") for batch in batches],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def create(self, expected_count: int) -> UploadBatch:
        with _UPLOAD_BATCH_LOCK:
            if expected_count < 1:
                raise LocalUploadBatchFailure("invalid_expected_count")
            now = datetime.now(UTC)
            batch = UploadBatch(
                batch_id=f"BATCH-{now:%Y%m%d%H%M%S}-{uuid4().hex[:8].upper()}",
                expected_count=expected_count,
                items=[],
                created_at=now,
                updated_at=now,
            )
            batches = self._read()
            batches.append(batch)
            self._write(batches)
            return batch

    def get(self, batch_id: str) -> UploadBatch | None:
        with _UPLOAD_BATCH_LOCK:
            batch = next((item for item in self._read() if item.batch_id == batch_id), None)
            if batch is None:
                return None
            return batch.model_copy(
                update={"items": sorted(batch.items, key=lambda item: item.position)}
            )

    def list_batches(self) -> list[UploadBatch]:
        with _UPLOAD_BATCH_LOCK:
            return [
                batch.model_copy(
                    update={"items": sorted(batch.items, key=lambda item: item.position)}
                )
                for batch in sorted(
                    self._read(),
                    key=lambda batch: (batch.created_at, batch.batch_id),
                )
            ]

    def batch_confirmation_version(self, batch_id: str) -> int | None:
        with _UPLOAD_BATCH_LOCK:
            batch = next((item for item in self._read() if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            return _batch_confirmation_version(batch)

    def _bind_style_test_product_in_batches(
        self,
        batches: list[UploadBatch],
        batch: UploadBatch,
        *,
        customer_test_id: str,
        product_id: str,
        product_category: str,
        product_form: str,
        force_new_version: bool = False,
        reason: StyleTestConfirmationVersionReason = "product_binding",
    ) -> tuple[UploadBatch, int]:
        proposed = StyleTestProductDraftBindingFact(
            customer_test_id=customer_test_id,
            product_id=product_id,
            product_category=product_category,
            product_form=product_form,
            recorded_at=datetime.now(UTC),
        )
        existing_binding = (
            batch.style_test_product_confirmation or batch.style_test_product_draft_binding
        )
        if existing_binding is not None:
            comparable = (
                "customer_test_id",
                "product_id",
                "product_category",
                "product_form",
            )
            if any(getattr(existing_binding, key) != getattr(proposed, key) for key in comparable):
                raise LocalUploadBatchFailure("target_product_binding_conflict")
        existing_reservations = [
            fact for fact in batch.style_test_confirmation_versions if fact.product_id == product_id
        ]
        active_versions = _active_item_confirmation_versions(batch)
        if existing_reservations:
            current_reservation = max(fact.confirmation_version for fact in existing_reservations)
            permission_aligned = bool(
                batch.style_test_permission is None
                or batch.style_test_permission.confirmation_version == current_reservation
            )
            product_aligned = bool(
                batch.style_test_product_confirmation is None
                or batch.style_test_product_confirmation.confirmation_version == current_reservation
            )
            if (
                not force_new_version
                and all(version == current_reservation for version in active_versions)
                and permission_aligned
                and product_aligned
            ):
                return batch, current_reservation

        global_version = _product_confirmation_version_max(batches, product_id)
        current_batch_version = _batch_confirmation_version(batch) or 1
        if existing_reservations or force_new_version:
            confirmation_version = max(global_version, current_batch_version) + 1
        else:
            confirmation_version = max(global_version + 1, current_batch_version)
        bound = batch.model_copy(
            update={
                "style_test_product_draft_binding": (
                    batch.style_test_product_draft_binding
                    or (proposed if batch.style_test_product_confirmation is None else None)
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        carried = _carry_item_facts_to_version(bound, confirmation_version)
        updated = _record_bound_product_version(
            carried,
            product_id=product_id,
            confirmation_version=confirmation_version,
            reason=reason,
        )
        return updated, confirmation_version

    def _advance_bound_payload_version(
        self,
        batches: list[UploadBatch],
        batch: UploadBatch,
        *,
        reason: StyleTestConfirmationVersionReason = "payload_mutation",
    ) -> tuple[UploadBatch, int | None]:
        """Give one changed, product-bound batch a fresh global version in memory."""

        binding = batch.style_test_product_confirmation or batch.style_test_product_draft_binding
        if binding is None:
            return batch, None
        confirmation_version = (
            max(
                _product_confirmation_version_max(batches, binding.product_id),
                _batch_confirmation_version(batch) or 0,
            )
            + 1
        )
        carried = _carry_item_facts_to_version(batch, confirmation_version)
        return (
            _record_bound_product_version(
                carried,
                product_id=binding.product_id,
                confirmation_version=confirmation_version,
                reason=reason,
            ),
            confirmation_version,
        )

    def bind_style_test_product(
        self,
        batch_id: str,
        *,
        customer_test_id: str,
        product_id: str,
        product_category: str,
        product_form: str,
    ) -> int:
        """Atomically reserve one durable, monotonically increasing product version."""

        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            updated, version = self._bind_style_test_product_in_batches(
                batches,
                batch,
                customer_test_id=customer_test_id,
                product_id=product_id,
                product_category=product_category,
                product_form=product_form,
            )
            if updated != batch:
                self._write([updated if item.batch_id == batch_id else item for item in batches])
            return version

    def _replace(self, batch: UploadBatch) -> UploadBatch:
        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            for index, item in enumerate(batches):
                if item.batch_id == batch.batch_id:
                    batches[index] = batch
                    self._write(batches)
                    return batch
            raise LocalUploadBatchFailure("batch_not_found")

    def digest_item(self, batch_id: str, sha256: str) -> UploadBatchItem | None:
        with _UPLOAD_BATCH_LOCK:
            batch = self.get(batch_id)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            return next(
                (item for item in batch.items if item.sha256 == sha256 and item.status != "failed"),
                None,
            )

    def _position_retry(
        self,
        batch: UploadBatch,
        position: int,
        sha256: str,
    ) -> UploadBatchItem | None:
        existing = next((item for item in batch.items if item.position == position), None)
        if existing is None:
            return None
        if existing.sha256 == sha256:
            return existing
        raise LocalUploadBatchFailure("position_conflict")

    def add_image(
        self,
        batch_id: str,
        position: int,
        filename: str,
        record: TemplateRecord,
        *,
        product_code: str | None = None,
        product_sequence: int | None = None,
        image_role: Literal["main", "detail"] | None = None,
        product_name: str | None = None,
        merchant_price_reference: str | None = None,
        source_system: Literal[
            "local_upload", "employee_workbook", "album_organizer"
        ] = "local_upload",
        initial_result: InitialRecognitionResult | None = None,
        replace_existing: bool = False,
    ) -> UploadBatchItem:
        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            existing = next(
                (item for item in batch.items if item.position == position),
                None,
            )
            replacement = (
                existing
                if replace_existing
                and existing is not None
                and existing.status in {"duplicate", "failed"}
                else None
            )
            if existing is not None and replacement is None:
                retried = self._position_retry(batch, position, record.sha256)
                if retried is not None:
                    return retried
            base_items = [
                item
                for item in batch.items
                if replacement is None or item.item_id != replacement.item_id
            ]
            duplicate = next(
                (
                    item
                    for item in base_items
                    if item.sha256 == record.sha256 and item.status != "failed"
                ),
                None,
            )
            if duplicate is not None:
                return self.add_duplicate(
                    batch_id,
                    position,
                    filename,
                    record.sha256,
                    record.mime_type,
                    duplicate,
                    product_code=product_code,
                    product_sequence=product_sequence,
                    image_role=image_role,
                    product_name=product_name,
                    merchant_price_reference=merchant_price_reference,
                    source_system=source_system,
                    replace_existing=replace_existing,
                )
            item = UploadBatchItem(
                item_id=uuid4(),
                position=position,
                filename=filename,
                status="pending_review",
                template_code=record.template_code,
                sha256=record.sha256,
                mime_type=record.mime_type,
                object_key=record.object_key,
                product_code=product_code,
                product_sequence=product_sequence,
                image_role=image_role,
                product_name=product_name,
                merchant_price_reference=merchant_price_reference,
                source_system=source_system,
                initial_result=initial_result or InitialRecognitionResult(),
                created_at=datetime.now(UTC),
            )
            updated = batch.model_copy(
                update={
                    "items": [*base_items, item],
                    "item_audit": (
                        *batch.item_audit,
                        *(
                            (
                                UploadBatchItemAudit(
                                    action="replaced",
                                    item_id=replacement.item_id,
                                    from_position=replacement.position,
                                    recorded_at=datetime.now(UTC),
                                ),
                            )
                            if replacement is not None
                            else ()
                        ),
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            self._write([updated if batch.batch_id == batch_id else batch for batch in batches])
            return item

    def add_classified_image(
        self,
        batch_id: str,
        position: int,
        filename: str,
        record: TemplateRecord,
        *,
        automatic_decision: PhotoUsageDecision,
        product_code: str | None = None,
        product_sequence: int | None = None,
        image_role: Literal["main", "detail"] | None = None,
        product_name: str | None = None,
        merchant_price_reference: str | None = None,
        source_system: Literal[
            "local_upload", "employee_workbook", "album_organizer"
        ] = "local_upload",
        initial_result: InitialRecognitionResult | None = None,
        replace_existing: bool = False,
    ) -> UploadBatchItem:
        """Store an image and its first classification as one versioned mutation."""

        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            existing = next(
                (item for item in batch.items if item.position == position),
                None,
            )
            replacement = (
                existing
                if replace_existing
                and existing is not None
                and existing.status in {"duplicate", "failed"}
                else None
            )
            if existing is not None and replacement is None:
                retried = self._position_retry(batch, position, record.sha256)
                if retried is not None:
                    return retried
            base_items = [
                item
                for item in batch.items
                if replacement is None or item.item_id != replacement.item_id
            ]
            duplicate = next(
                (
                    item
                    for item in base_items
                    if item.sha256 == record.sha256 and item.status != "failed"
                ),
                None,
            )
            if duplicate is not None:
                return self.add_duplicate(
                    batch_id,
                    position,
                    filename,
                    record.sha256,
                    record.mime_type,
                    duplicate,
                    product_code=product_code,
                    product_sequence=product_sequence,
                    image_role=image_role,
                    product_name=product_name,
                    merchant_price_reference=merchant_price_reference,
                    source_system=source_system,
                    replace_existing=replace_existing,
                )
            fact = PhotoClassificationConfirmationFact(
                automatic_decision=automatic_decision,
                human_correction=None,
                final_category=automatic_decision.category,
                confirmation_version=_batch_confirmation_version(batch) or 1,
            )
            item = UploadBatchItem(
                item_id=uuid4(),
                position=position,
                filename=filename,
                status="pending_review",
                template_code=record.template_code,
                sha256=record.sha256,
                mime_type=record.mime_type,
                object_key=record.object_key,
                product_code=product_code,
                product_sequence=product_sequence,
                image_role=image_role,
                product_name=product_name,
                merchant_price_reference=merchant_price_reference,
                source_system=source_system,
                photo_classification_facts=(fact,),
                initial_result=initial_result or InitialRecognitionResult(),
                created_at=datetime.now(UTC),
            )
            updated = batch.model_copy(
                update={
                    "items": [*base_items, item],
                    "item_audit": (
                        *batch.item_audit,
                        *(
                            (
                                UploadBatchItemAudit(
                                    action="replaced",
                                    item_id=replacement.item_id,
                                    from_position=replacement.position,
                                    recorded_at=datetime.now(UTC),
                                ),
                            )
                            if replacement is not None
                            else ()
                        ),
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            updated, _ = self._advance_bound_payload_version(batches, updated)
            self._write([updated if batch.batch_id == batch_id else batch for batch in batches])
            return next(entry for entry in updated.items if entry.item_id == item.item_id)

    def add_duplicate(
        self,
        batch_id: str,
        position: int,
        filename: str,
        sha256: str,
        mime_type: str,
        original: UploadBatchItem,
        *,
        product_code: str | None = None,
        product_sequence: int | None = None,
        image_role: Literal["main", "detail"] | None = None,
        product_name: str | None = None,
        merchant_price_reference: str | None = None,
        source_system: Literal[
            "local_upload", "employee_workbook", "album_organizer"
        ] = "local_upload",
        replace_existing: bool = False,
    ) -> UploadBatchItem:
        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            existing = next(
                (item for item in batch.items if item.position == position),
                None,
            )
            replacement = (
                existing
                if replace_existing
                and existing is not None
                and existing.status in {"duplicate", "failed"}
                else None
            )
            if existing is not None and replacement is None:
                retried = self._position_retry(batch, position, sha256)
                if retried is not None:
                    return retried
            base_items = [
                entry
                for entry in batch.items
                if replacement is None or entry.item_id != replacement.item_id
            ]
            if original.item_id not in {entry.item_id for entry in base_items}:
                raise LocalUploadBatchFailure("image_not_found")
            item = UploadBatchItem(
                item_id=uuid4(),
                position=position,
                filename=filename,
                status="duplicate",
                template_code=original.template_code,
                sha256=sha256,
                mime_type=mime_type,
                object_key=original.object_key,
                product_code=product_code,
                product_sequence=product_sequence,
                image_role=image_role,
                product_name=product_name,
                merchant_price_reference=merchant_price_reference,
                source_system=source_system,
                duplicate_of_item_id=original.item_id,
                initial_result=original.initial_result,
                recognition_status=original.recognition_status,
                photo_classification_facts=original.photo_classification_facts,
                created_at=datetime.now(UTC),
            )
            updated = batch.model_copy(
                update={
                    "items": [*base_items, item],
                    "item_audit": (
                        *batch.item_audit,
                        *(
                            (
                                UploadBatchItemAudit(
                                    action="replaced",
                                    item_id=replacement.item_id,
                                    from_position=replacement.position,
                                    recorded_at=datetime.now(UTC),
                                ),
                            )
                            if replacement is not None
                            else ()
                        ),
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            updated, _ = self._advance_bound_payload_version(batches, updated)
            self._write([updated if batch.batch_id == batch_id else batch for batch in batches])
            return next(entry for entry in updated.items if entry.item_id == item.item_id)

    def mark_failed(
        self,
        batch_id: str,
        position: int,
        filename: str,
        sha256: str | None,
        error_code: str,
        *,
        replace_existing: bool = False,
    ) -> UploadBatchItem:
        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            existing = next((item for item in batch.items if item.position == position), None)
            if existing is not None and existing.status != "failed" and not replace_existing:
                raise LocalUploadBatchFailure("position_conflict")
            if (
                existing is not None
                and existing.status == "failed"
                and existing.filename == filename
                and existing.sha256 == sha256
                and existing.error_code == error_code
            ):
                return existing
            item = UploadBatchItem(
                item_id=existing.item_id if existing else uuid4(),
                position=position,
                filename=filename,
                status="failed",
                template_code=None,
                sha256=sha256,
                mime_type=None,
                object_key=None,
                error_code=error_code,
                photo_classification_facts=existing.photo_classification_facts if existing else (),
                created_at=existing.created_at if existing else datetime.now(UTC),
            )
            items = [entry for entry in batch.items if entry.position != position]
            updated = batch.model_copy(
                update={
                    "items": [*items, item],
                    "item_audit": (
                        *batch.item_audit,
                        *(
                            (
                                UploadBatchItemAudit(
                                    action="replaced",
                                    item_id=existing.item_id,
                                    from_position=existing.position,
                                    recorded_at=datetime.now(UTC),
                                ),
                            )
                            if existing is not None
                            else ()
                        ),
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            updated, _ = self._advance_bound_payload_version(batches, updated)
            self._write([updated if batch.batch_id == batch_id else batch for batch in batches])
            return next(entry for entry in updated.items if entry.item_id == item.item_id)

    def mark_reviewed(self, batch_id: str, item_id: UUID) -> UploadBatchItem:
        with _UPLOAD_BATCH_LOCK:
            batch = self.get(batch_id)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            item = next((entry for entry in batch.items if entry.item_id == item_id), None)
            if item is None or item.status not in {"pending_review", "reviewed"}:
                raise LocalUploadBatchFailure("item_not_reviewable")
            reviewed = item.model_copy(update={"status": "reviewed"})
            items = [reviewed if entry.item_id == item_id else entry for entry in batch.items]
            self._replace(
                batch.model_copy(update={"items": items, "updated_at": datetime.now(UTC)})
            )
            return reviewed

    def mark_product_reviewed(self, batch_id: str, product_code: str) -> list[UploadBatchItem]:
        with _UPLOAD_BATCH_LOCK:
            batch = self.get(batch_id)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            matching = [
                entry
                for entry in batch.items
                if entry.product_code == product_code
                and entry.status in {"pending_review", "reviewed"}
            ]
            if not matching:
                raise LocalUploadBatchFailure("item_not_reviewable")
            matching_ids = {entry.item_id for entry in matching}
            reviewed_items = [
                entry.model_copy(update={"status": "reviewed"})
                if entry.item_id in matching_ids
                else entry
                for entry in batch.items
            ]
            self._replace(
                batch.model_copy(update={"items": reviewed_items, "updated_at": datetime.now(UTC)})
            )
            return [entry for entry in reviewed_items if entry.item_id in matching_ids]

    def remove_item(
        self, batch_id: str, item_id: UUID, *, action: Literal["removed", "replaced"] = "removed"
    ) -> UploadBatchItem:
        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            item = next((entry for entry in batch.items if entry.item_id == item_id), None)
            if item is None:
                raise LocalUploadBatchFailure("image_not_found")
            audit = UploadBatchItemAudit(
                action=action,
                item_id=item.item_id,
                from_position=item.position,
                recorded_at=datetime.now(UTC),
            )
            updated = batch.model_copy(
                update={
                    "items": _recompute_duplicate_relationships(
                        [entry for entry in batch.items if entry.item_id != item_id]
                    ),
                    "item_audit": (*batch.item_audit, audit),
                    "updated_at": datetime.now(UTC),
                }
            )
            updated, _ = self._advance_bound_payload_version(batches, updated)
            self._write([updated if batch.batch_id == batch_id else batch for batch in batches])
            return item

    def move_item(self, batch_id: str, item_id: UUID, to_position: int) -> UploadBatchItem:
        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            item = next((entry for entry in batch.items if entry.item_id == item_id), None)
            if item is None or to_position < 0:
                raise LocalUploadBatchFailure("image_not_found")
            if any(
                entry.position == to_position and entry.item_id != item_id for entry in batch.items
            ):
                raise LocalUploadBatchFailure("position_conflict")
            moved = item.model_copy(update={"position": to_position})
            audit = UploadBatchItemAudit(
                action="moved",
                item_id=item.item_id,
                from_position=item.position,
                to_position=to_position,
                recorded_at=datetime.now(UTC),
            )
            updated = batch.model_copy(
                update={
                    "items": [
                        moved if entry.item_id == item_id else entry for entry in batch.items
                    ],
                    "item_audit": (*batch.item_audit, audit),
                    "updated_at": datetime.now(UTC),
                }
            )
            updated, _ = self._advance_bound_payload_version(batches, updated)
            self._write([updated if batch.batch_id == batch_id else batch for batch in batches])
            return next(entry for entry in updated.items if entry.item_id == item_id)

    def move_item_to_batch(
        self,
        source_batch_id: str,
        item_id: UUID,
        target_batch_id: str,
        to_position: int | None = None,
        *,
        target_customer_test_id: str | None = None,
        target_product_id: str | None = None,
        target_product_category: str | None = None,
        target_product_form: str | None = None,
    ) -> UploadBatchItem:
        """Move bytes metadata under one lock; source receives an audit tombstone."""
        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            source = next((batch for batch in batches if batch.batch_id == source_batch_id), None)
            target = next((batch for batch in batches if batch.batch_id == target_batch_id), None)
            if source is None or target is None or source_batch_id == target_batch_id:
                raise LocalUploadBatchFailure("target_batch_not_found")
            item = next((entry for entry in source.items if entry.item_id == item_id), None)
            if item is None:
                raise LocalUploadBatchFailure("image_not_found")
            target_position = (
                max((entry.position for entry in target.items), default=-1) + 1
                if to_position is None
                else to_position
            )
            if target_position < 0:
                raise LocalUploadBatchFailure("image_not_found")
            if any(entry.position == target_position for entry in target.items):
                raise LocalUploadBatchFailure("position_conflict")
            binding_values = (
                target_customer_test_id,
                target_product_id,
                target_product_category,
                target_product_form,
            )
            persisted_target_binding = (
                target.style_test_product_confirmation or target.style_test_product_draft_binding
            )
            target_binding: StyleTestProductDraftBindingFact | None = None
            if any(value is not None for value in binding_values):
                if not all(isinstance(value, str) and value.strip() for value in binding_values):
                    raise LocalUploadBatchFailure("style_test_product_binding_invalid")
                target_binding = StyleTestProductDraftBindingFact(
                    customer_test_id=cast(str, target_customer_test_id),
                    product_id=cast(str, target_product_id),
                    product_category=cast(str, target_product_category),
                    product_form=cast(str, target_product_form),
                    recorded_at=datetime.now(UTC),
                )
                if persisted_target_binding is not None:
                    comparable = (
                        "customer_test_id",
                        "product_id",
                        "product_category",
                        "product_form",
                    )
                    if any(
                        getattr(persisted_target_binding, key) != getattr(target_binding, key)
                        for key in comparable
                    ):
                        raise LocalUploadBatchFailure("target_product_binding_conflict")
            moved = item.model_copy(
                update={
                    "position": target_position,
                }
            )
            source_items = _recompute_duplicate_relationships(
                [entry for entry in source.items if entry.item_id != item_id]
            )
            target_items = _recompute_duplicate_relationships([*target.items, moved])
            moved = next(entry for entry in target_items if entry.item_id == item_id)
            audit = UploadBatchItemAudit(
                action="moved",
                item_id=item.item_id,
                from_position=item.position,
                to_position=moved.position,
                recorded_at=datetime.now(UTC),
            )
            updated_source = source.model_copy(
                update={
                    "items": source_items,
                    "item_audit": (*source.item_audit, audit),
                    "updated_at": datetime.now(UTC),
                }
            )
            updated_target = target.model_copy(
                update={
                    "items": target_items,
                    "item_audit": (*target.item_audit, audit),
                    "style_test_product_draft_binding": (
                        target.style_test_product_draft_binding
                        or (
                            target_binding
                            if target.style_test_product_confirmation is None
                            else None
                        )
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            updated_source, _ = self._advance_bound_payload_version(
                batches,
                updated_source,
                reason="cross_batch_move",
            )
            allocation_batches = [
                updated_source if batch.batch_id == source_batch_id else batch for batch in batches
            ]
            effective_binding = target_binding or persisted_target_binding
            if effective_binding is not None:
                if persisted_target_binding is not None:
                    updated_target, _ = self._advance_bound_payload_version(
                        allocation_batches,
                        updated_target,
                        reason="cross_batch_move",
                    )
                else:
                    updated_target, _ = self._bind_style_test_product_in_batches(
                        allocation_batches,
                        updated_target,
                        customer_test_id=effective_binding.customer_test_id,
                        product_id=effective_binding.product_id,
                        product_category=effective_binding.product_category,
                        product_form=effective_binding.product_form,
                        reason="cross_batch_move",
                    )
            self._write(
                [
                    updated_target if batch.batch_id == target_batch_id else batch
                    for batch in allocation_batches
                ]
            )
            return next(entry for entry in updated_target.items if entry.item_id == item_id)

    def set_recognition_status(
        self,
        batch_id: str,
        item_id: UUID,
        status: Literal["pending", "succeeded", "fallback_exact_match", "unavailable", "failed"],
    ) -> UploadBatchItem:
        with _UPLOAD_BATCH_LOCK:
            batch = self.get(batch_id)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            item = next((entry for entry in batch.items if entry.item_id == item_id), None)
            if item is None:
                raise LocalUploadBatchFailure("image_not_found")
            updated = item.model_copy(update={"recognition_status": status})
            items = [updated if entry.item_id == item_id else entry for entry in batch.items]
            self._replace(
                batch.model_copy(update={"items": items, "updated_at": datetime.now(UTC)})
            )
            return updated

    def append_photo_classification_fact(
        self,
        batch_id: str,
        item_id: UUID,
        *,
        automatic_decision: PhotoUsageDecision,
        human_correction: PhotoUsageCorrectionEvent | None,
        final_category: PhotoCategory,
        confirmation_version: int,
    ) -> PhotoClassificationConfirmationFact:
        """Append a fact; retries reuse an identical fact and conflicts never overwrite it."""

        with _UPLOAD_BATCH_LOCK:
            batch = self.get(batch_id)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            item = next((entry for entry in batch.items if entry.item_id == item_id), None)
            if item is None:
                raise LocalUploadBatchFailure("image_not_found")
            if item.status in {"duplicate", "failed"}:
                raise LocalUploadBatchFailure("item_not_confirmable")
            if type(confirmation_version) is not int or confirmation_version < 1:
                raise LocalUploadBatchFailure("invalid_confirmation_version")
            fact = PhotoClassificationConfirmationFact(
                automatic_decision=automatic_decision,
                human_correction=human_correction,
                final_category=final_category,
                confirmation_version=confirmation_version,
            )
            if item.photo_classification_facts:
                original = item.photo_classification_facts[0].automatic_decision
                if fact.automatic_decision != original or (
                    fact.human_correction is not None
                    and fact.human_correction.automatic_decision != original
                ):
                    raise LocalUploadBatchFailure("automatic_decision_conflict")
            existing = next(
                (
                    entry
                    for entry in item.photo_classification_facts
                    if entry.confirmation_version == confirmation_version
                ),
                None,
            )
            if existing is not None:
                if existing == fact:
                    return existing
                raise LocalUploadBatchFailure("confirmation_version_conflict")
            updated = item.model_copy(
                update={
                    "photo_classification_facts": tuple(
                        sorted(
                            (*item.photo_classification_facts, fact),
                            key=lambda entry: entry.confirmation_version,
                        )
                    )
                }
            )
            items = [updated if entry.item_id == item_id else entry for entry in batch.items]
            self._replace(
                batch.model_copy(update={"items": items, "updated_at": datetime.now(UTC)})
            )
            return fact

    def apply_photo_classification_correction(
        self,
        batch_id: str,
        item_id: UUID,
        *,
        corrected_category: PhotoCategory,
        reviewer_note: str,
    ) -> int:
        """Append one semantic correction and carry every active item to one new version."""

        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            active = [item for item in batch.items if item.status in {"pending_review", "reviewed"}]
            target = next((item for item in active if item.item_id == item_id), None)
            if target is None:
                raise LocalUploadBatchFailure("item_not_confirmable")
            if any(not item.photo_classification_facts for item in active):
                raise LocalUploadBatchFailure("photo_classification_required")
            original = target.photo_classification_facts[0].automatic_decision
            try:
                correction = record_photo_usage_correction(
                    automatic_decision=original,
                    corrected_category=corrected_category,
                    reviewer_note=reviewer_note,
                )
            except ValueError as error:
                raise LocalUploadBatchFailure(str(error)) from error
            latest_target = target.photo_classification_facts[-1]
            if (
                latest_target.human_correction == correction
                and latest_target.final_category == correction.corrected_category
            ):
                return latest_target.confirmation_version

            binding = (
                batch.style_test_product_confirmation or batch.style_test_product_draft_binding
            )
            current_batch_version = _batch_confirmation_version(batch) or 0
            if binding is None:
                confirmation_version = current_batch_version + 1
            else:
                confirmation_version = (
                    max(
                        current_batch_version,
                        _product_confirmation_version_max(batches, binding.product_id),
                    )
                    + 1
                )
            updated_items: list[UploadBatchItem] = []
            for item in batch.items:
                if item.status not in {"pending_review", "reviewed"}:
                    updated_items.append(item)
                    continue
                latest = item.photo_classification_facts[-1]
                fact = PhotoClassificationConfirmationFact(
                    automatic_decision=latest.automatic_decision,
                    human_correction=(
                        correction if item.item_id == item_id else latest.human_correction
                    ),
                    final_category=(
                        correction.corrected_category
                        if item.item_id == item_id
                        else latest.final_category
                    ),
                    classifier_version=latest.classifier_version,
                    confirmation_version=confirmation_version,
                )
                updated_items.append(
                    item.model_copy(
                        update={
                            "photo_classification_facts": (
                                *item.photo_classification_facts,
                                fact,
                            )
                        }
                    )
                )
            updated = batch.model_copy(
                update={
                    "items": updated_items,
                    "updated_at": datetime.now(UTC),
                }
            )
            if binding is not None:
                updated = _record_bound_product_version(
                    updated,
                    product_id=binding.product_id,
                    confirmation_version=confirmation_version,
                    reason="classification_correction",
                )
            self._write([updated if item.batch_id == batch_id else item for item in batches])
            return confirmation_version

    def confirm_style_test_batch(
        self,
        batch_id: str,
        *,
        customer_test_id: str,
        product_id: str,
        product_category: str,
        product_form: str,
        main_photo_id: UUID,
        transform_permission: bool,
    ) -> int:
        """Write aligned permission and product facts together or write neither."""

        with _UPLOAD_BATCH_LOCK:
            batches = self._read()
            batch = next((item for item in batches if item.batch_id == batch_id), None)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            binding = batch.style_test_product_draft_binding
            if binding is None or any(
                getattr(binding, key) != value
                for key, value in (
                    ("customer_test_id", customer_test_id),
                    ("product_id", product_id),
                    ("product_category", product_category),
                    ("product_form", product_form),
                )
            ):
                raise LocalUploadBatchFailure("target_product_binding_conflict")
            confirmation_version = _batch_confirmation_version(batch)
            if confirmation_version is None:
                raise LocalUploadBatchFailure("photo_classification_required")
            active = [item for item in batch.items if item.status in {"pending_review", "reviewed"}]
            if not active or any(
                not item.photo_classification_facts
                or item.photo_classification_facts[-1].confirmation_version != confirmation_version
                for item in active
            ):
                raise LocalUploadBatchFailure("photo_classification_required")
            if not any(item.item_id == main_photo_id for item in active):
                raise LocalUploadBatchFailure("main_photo_required")
            permission = StyleTestPermissionFact(
                customer_test_id=customer_test_id,
                product_id=product_id,
                confirmation_version=confirmation_version,
                transform_permission=transform_permission,
                recorded_at=datetime.now(UTC),
            )
            product = StyleTestProductConfirmationFact(
                customer_test_id=customer_test_id,
                product_id=product_id,
                product_category=product_category,
                product_form=product_form,
                confirmation_version=confirmation_version,
                main_photo_id=main_photo_id,
                recorded_at=datetime.now(UTC),
            )
            if (
                batch.style_test_permission is not None
                or batch.style_test_product_confirmation is not None
            ):
                existing_permission = batch.style_test_permission
                existing_product = batch.style_test_product_confirmation
                permission_matches = bool(
                    existing_permission
                    and existing_permission.customer_test_id == permission.customer_test_id
                    and existing_permission.product_id == permission.product_id
                    and existing_permission.confirmation_version == permission.confirmation_version
                    and existing_permission.transform_permission == permission.transform_permission
                )
                comparable = (
                    "customer_test_id",
                    "product_id",
                    "product_category",
                    "product_form",
                    "confirmation_version",
                    "main_photo_id",
                )
                product_matches = bool(
                    existing_product
                    and all(
                        getattr(existing_product, key) == getattr(product, key)
                        for key in comparable
                    )
                )
                if permission_matches and product_matches:
                    return confirmation_version
                raise LocalUploadBatchFailure("style_test_product_confirmation_conflict")
            updated = batch.model_copy(
                update={
                    "style_test_permission": permission,
                    "style_test_product_confirmation": product,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._write([updated if item.batch_id == batch_id else item for item in batches])
            return confirmation_version

    def set_style_test_permission(
        self,
        batch_id: str,
        *,
        customer_test_id: str,
        product_id: str,
        confirmation_version: int,
        transform_permission: bool,
    ) -> StyleTestPermissionFact:
        """Record one explicit permission fact; a changed fact never overwrites it."""

        with _UPLOAD_BATCH_LOCK:
            batch = self.get(batch_id)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            fact = StyleTestPermissionFact(
                customer_test_id=customer_test_id,
                product_id=product_id,
                confirmation_version=confirmation_version,
                transform_permission=transform_permission,
                recorded_at=datetime.now(UTC),
            )
            if batch.style_test_permission is not None:
                existing = batch.style_test_permission
                if (
                    existing.customer_test_id == fact.customer_test_id
                    and existing.product_id == fact.product_id
                    and existing.confirmation_version == fact.confirmation_version
                    and existing.transform_permission == fact.transform_permission
                ):
                    return existing
                raise LocalUploadBatchFailure("style_test_permission_conflict")
            self._replace(
                batch.model_copy(
                    update={
                        "style_test_permission": fact,
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
            return fact

    def set_style_test_product_confirmation(
        self,
        batch_id: str,
        *,
        customer_test_id: str,
        product_id: str,
        product_category: str,
        product_form: str,
        confirmation_version: int,
        main_photo_id: UUID,
    ) -> StyleTestProductConfirmationFact:
        with _UPLOAD_BATCH_LOCK:
            batch = self.get(batch_id)
            if batch is None:
                raise LocalUploadBatchFailure("batch_not_found")
            fact = StyleTestProductConfirmationFact(
                customer_test_id=customer_test_id,
                product_id=product_id,
                product_category=product_category,
                product_form=product_form,
                confirmation_version=confirmation_version,
                main_photo_id=main_photo_id,
                recorded_at=datetime.now(UTC),
            )
            existing = batch.style_test_product_confirmation
            if existing is not None:
                comparable = (
                    "customer_test_id",
                    "product_id",
                    "product_category",
                    "product_form",
                    "confirmation_version",
                    "main_photo_id",
                )
                if all(getattr(existing, key) == getattr(fact, key) for key in comparable):
                    return existing
                raise LocalUploadBatchFailure("style_test_product_confirmation_conflict")
            self._replace(
                batch.model_copy(
                    update={
                        "style_test_product_confirmation": fact,
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
            return fact
