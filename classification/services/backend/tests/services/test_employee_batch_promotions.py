import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest

from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.contracts.employee_formal_handoff import (
    EmployeeFormalHandoffReceipt,
    EmployeePromotionProductReceipt,
)
from product_engine.services.employee_batch_promotions import (
    EmployeeBatchPromotionFailure,
    EmployeeBatchPromotionService,
)
from product_engine.services.employee_handoff_receipts import EmployeeHandoffReceiptStore
from product_engine.services.local_upload_batches import UploadBatch, UploadBatchItem
from product_engine.storage.protocol import StoredObject

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("22222222-2222-4222-8222-222222222222")
BATCH_ID = "BATCH-20260921035620-3A284AA4"
PRODUCTS = (
    ("BCNHDIY914", "耳饰", "earring"),
    ("BCNHDIY933", "未知", "unknown"),
    ("BCNHDIY951", "项链", "necklace"),
    ("BCNHDIY952", "手串", "bracelet"),
    ("BCNHDIY997", "手串", "bracelet"),
    ("BCNHDIY1047", "未知", "unknown"),
)


@dataclass(frozen=True)
class FakeTrainingVersion:
    change_source: Literal[
        "system_initial", "exact_match_reused", "trainer_confirmed", "trainer_rollback"
    ]
    product_form: str


class FakeBatchStore:
    def __init__(self, batch: UploadBatch) -> None:
        self.batch = batch

    def get(self, batch_id: str) -> UploadBatch | None:
        return self.batch if batch_id == self.batch.batch_id else None


class FakeTrainingStore:
    def __init__(self, versions: dict[str, FakeTrainingVersion]) -> None:
        self.versions = versions

    def current(self, template_code: str) -> FakeTrainingVersion | None:
        return self.versions.get(template_code)


class FakeSourceStorage:
    def __init__(self, content: dict[str, bytes]) -> None:
        self.content = content

    async def read(self, object_key: str) -> bytes:
        return self.content[object_key]


class FakeDestinationStorage:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes, str, str]] = []

    async def write_verified(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        sha256: str,
    ) -> StoredObject:
        self.writes.append((object_key, content, mime_type, sha256))
        return StoredObject(object_key, mime_type, len(content), sha256)


class FakeDatabaseWriter:
    def __init__(self) -> None:
        self.events: list[ProductImageSetReadyV1] = []

    async def commit_batch(
        self,
        *,
        request_id: UUID,
        batch_id: str,
        content_sha256: str,
        events: tuple[ProductImageSetReadyV1, ...],
    ) -> tuple[EmployeePromotionProductReceipt, ...]:
        del request_id, batch_id, content_sha256
        self.events.extend(events)
        return tuple(
            EmployeePromotionProductReceipt.from_event(event, package_version=index)
            for index, event in enumerate(events, start=1)
        )


class FakeProvenanceStore:
    def __init__(self) -> None:
        self.receipts: list[EmployeeFormalHandoffReceipt] = []

    def save_employee_receipt(self, receipt: EmployeeFormalHandoffReceipt) -> None:
        self.receipts.append(receipt)


def reviewed_six_product_batch() -> tuple[UploadBatch, dict[str, bytes]]:
    items: list[UploadBatchItem] = []
    content: dict[str, bytes] = {}
    for product_index, (product_code, _, _) in enumerate(PRODUCTS, start=1):
        for image_index, role in enumerate(("main", "detail"), start=1):
            object_key = f"upload-batches/{BATCH_ID}/{product_code}-{image_index}.jpg"
            image = f"{product_code}-{image_index}".encode()
            content[object_key] = image
            items.append(
                UploadBatchItem(
                    item_id=UUID(int=(product_index * 10) + image_index),
                    position=len(items),
                    filename=f"{product_code}-{image_index}.jpg",
                    status="reviewed",
                    recognition_status="succeeded",
                    template_code=f"T-{product_code}-{image_index}",
                    sha256=hashlib.sha256(image).hexdigest(),
                    mime_type="image/jpeg",
                    object_key=object_key,
                    product_code=product_code,
                    product_sequence=product_index,
                    image_role=role,
                    product_name=product_code,
                    source_system="employee_workbook",
                    created_at=datetime(2026, 9, 21, tzinfo=UTC),
                )
            )
    return (
        UploadBatch(
            batch_id=BATCH_ID,
            expected_count=12,
            items=items,
            created_at=datetime(2026, 9, 21, tzinfo=UTC),
            updated_at=datetime(2026, 9, 21, tzinfo=UTC),
        ),
        content,
    )


def service_for(
    tmp_path: Path,
    batch: UploadBatch,
    content: dict[str, bytes],
    *,
    form_override: dict[str, str] | None = None,
) -> tuple[EmployeeBatchPromotionService, FakeDestinationStorage, FakeDatabaseWriter]:
    form_override = form_override or {}
    human_forms = {product_code: human for product_code, human, _ in PRODUCTS}
    versions = {
        item.template_code: FakeTrainingVersion(
            change_source="trainer_confirmed",
            product_form=form_override.get(item.product_code or "", human_forms[item.product_code]),
        )
        for item in batch.items
        if item.template_code is not None and item.product_code is not None
    }
    destination = FakeDestinationStorage()
    writer = FakeDatabaseWriter()
    return (
        EmployeeBatchPromotionService(
            tenant_id=TENANT_ID,
            batch_store=FakeBatchStore(batch),
            training_version_store=FakeTrainingStore(versions),
            source_storage=FakeSourceStorage(content),
            destination_storage=destination,
            database_writer=writer,
            receipts=EmployeeHandoffReceiptStore(tmp_path / "receipts.json"),
            provenance=FakeProvenanceStore(),
            now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
        ),
        destination,
        writer,
    )


async def test_reviewed_six_product_batch_builds_six_strict_events(tmp_path: Path) -> None:
    batch, content = reviewed_six_product_batch()
    service, destination, writer = service_for(tmp_path, batch, content)

    receipt = await service.promote(BATCH_ID, REQUEST_ID)

    assert [item.product_code for item in receipt.products] == [item[0] for item in PRODUCTS]
    assert [event.form for event in writer.events] == [item[2] for item in PRODUCTS]
    assert {event.category for event in writer.events} == {"信息不足"}
    assert {event.classification_confidence for event in writer.events} == {0.0}
    assert [[image.role for image in event.images] for event in writer.events] == [
        ["main_candidate", "gallery"]
    ] * 6
    assert len(destination.writes) == 12
    assert receipt.downstream_status == "not_sent"


async def test_one_product_level_confirmation_covers_its_reviewed_detail_image(
    tmp_path: Path,
) -> None:
    batch, content = reviewed_six_product_batch()
    service, destination, writer = service_for(tmp_path, batch, content)
    detail = batch.items[1]
    assert detail.template_code is not None
    service.training_version_store.versions[detail.template_code] = FakeTrainingVersion(
        change_source="exact_match_reused",
        product_form="待确认",
    )

    receipt = await service.promote(BATCH_ID, REQUEST_ID)

    assert receipt.products[0].product_code == "BCNHDIY914"
    assert writer.events[0].form == "earring"
    assert len(destination.writes) == 12


async def test_product_without_any_human_form_confirmation_still_fails(
    tmp_path: Path,
) -> None:
    batch, content = reviewed_six_product_batch()
    service, destination, writer = service_for(tmp_path, batch, content)
    for item in batch.items[:2]:
        assert item.template_code is not None
        service.training_version_store.versions[item.template_code] = FakeTrainingVersion(
            change_source="exact_match_reused",
            product_form="耳饰",
        )

    with pytest.raises(
        EmployeeBatchPromotionFailure, match="employee_product_form_unconfirmed"
    ):
        await service.promote(BATCH_ID, REQUEST_ID)
    assert destination.writes == []
    assert writer.events == []


async def test_pending_image_fails_before_storage_or_database(tmp_path: Path) -> None:
    batch, content = reviewed_six_product_batch()
    pending = batch.items[0].model_copy(update={"status": "pending_review"})
    batch = batch.model_copy(update={"items": [pending, *batch.items[1:]]})
    service, destination, writer = service_for(tmp_path, batch, content)
    with pytest.raises(EmployeeBatchPromotionFailure, match="employee_batch_review_incomplete"):
        await service.promote(BATCH_ID, REQUEST_ID)
    assert destination.writes == []
    assert writer.events == []


@pytest.mark.parametrize("roles", [("detail", "detail"), ("main", "main")])
async def test_invalid_main_image_count_fails_before_writes(
    tmp_path: Path,
    roles: tuple[str, str],
) -> None:
    batch, content = reviewed_six_product_batch()
    changed = [
        item.model_copy(update={"image_role": roles[index]}) if index < 2 else item
        for index, item in enumerate(batch.items)
    ]
    service, destination, writer = service_for(
        tmp_path,
        batch.model_copy(update={"items": changed}),
        content,
    )
    with pytest.raises(EmployeeBatchPromotionFailure, match="employee_product_main_image_invalid"):
        await service.promote(BATCH_ID, REQUEST_ID)
    assert destination.writes == []
    assert writer.events == []


async def test_blank_or_unconfirmed_form_fails_before_writes(tmp_path: Path) -> None:
    batch, content = reviewed_six_product_batch()
    service, destination, writer = service_for(
        tmp_path,
        batch,
        content,
        form_override={"BCNHDIY914": ""},
    )
    with pytest.raises(EmployeeBatchPromotionFailure, match="employee_product_form_unconfirmed"):
        await service.promote(BATCH_ID, REQUEST_ID)
    assert destination.writes == []
    assert writer.events == []


async def test_duplicate_item_identity_fails_before_writes(tmp_path: Path) -> None:
    batch, content = reviewed_six_product_batch()
    duplicate = batch.items[1].model_copy(update={"item_id": batch.items[0].item_id})
    service, destination, writer = service_for(
        tmp_path,
        batch.model_copy(update={"items": [batch.items[0], duplicate, *batch.items[2:]]}),
        content,
    )
    with pytest.raises(EmployeeBatchPromotionFailure, match="employee_handoff_ledger_conflict"):
        await service.promote(BATCH_ID, REQUEST_ID)
    assert destination.writes == []
    assert writer.events == []


async def test_changed_source_bytes_fail_before_database(tmp_path: Path) -> None:
    batch, content = reviewed_six_product_batch()
    content[batch.items[0].object_key or ""] = b"changed-after-review"
    service, destination, writer = service_for(tmp_path, batch, content)
    with pytest.raises(EmployeeBatchPromotionFailure, match="employee_batch_already_changed"):
        await service.promote(BATCH_ID, REQUEST_ID)
    assert destination.writes == []
    assert writer.events == []
