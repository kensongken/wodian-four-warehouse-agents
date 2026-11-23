import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest

from product_engine.services.local_upload_batches import UploadBatch, UploadBatchItem
from product_engine.services.runtime_classification_confirmations import (
    RuntimeClassificationConfirmationWriter,
)

BATCH_ID = "BATCH-20260921035620-3A284AA4"
REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


@dataclass(frozen=True)
class FakeTrainingVersion:
    change_source: Literal["trainer_confirmed", "trainer_rollback"]
    material_label: str
    product_form: str
    visual_observations: list[str]
    selling_points: list[str]
    inventory_status: Literal["in_stock", "reserved", "out_of_stock"] | None = None
    service_rules: tuple[object, ...] = ()


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


def build_writer(
    tmp_path: Path,
    *,
    product_code: str,
    human_form: str,
    source_type: str,
    include_source_type: bool = True,
) -> RuntimeClassificationConfirmationWriter:
    items = [
        UploadBatchItem(
            item_id=UUID(int=index + 1),
            position=index,
            filename=f"{product_code}-{index}.jpg",
            status="reviewed",
            recognition_status="succeeded",
            template_code=f"T-{index}",
            sha256=("a" if index == 0 else "b") * 64,
            mime_type="image/jpeg",
            object_key=f"local/{index}.jpg",
            product_code=product_code,
            product_sequence=1,
            image_role="main" if index == 0 else "detail",
            source_system="employee_workbook",
            created_at=datetime(2026, 9, 21, tzinfo=UTC),
        )
        for index in range(2)
    ]
    batch = UploadBatch(
        batch_id=BATCH_ID,
        expected_count=2,
        items=items,
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
        updated_at=datetime(2026, 9, 21, tzinfo=UTC),
    )
    provenance = {
        "batch_id": BATCH_ID,
        "assets": [
            {
                "item_id": str(item.item_id),
                "asset_id": str(UUID(int=100 + index)),
                "position": index,
                "role": "main_candidate" if index == 0 else "gallery",
                **({"sha256": item.sha256} if source_type == "employee_import_promotion" else {}),
            }
            for index, item in enumerate(items)
        ],
    }
    if include_source_type:
        provenance["source_type"] = source_type
    provenance_path = tmp_path / "classification-imports.json"
    provenance_path.write_text(
        json.dumps({f"{product_code}:v2": provenance}),
        encoding="utf-8",
    )
    versions = {
        item.template_code: FakeTrainingVersion(
            change_source="trainer_confirmed",
            material_label="绿松石",
            product_form=human_form,
            visual_observations=["可见商品轮廓"],
            selling_points=["高价值收藏承诺"],
            inventory_status="in_stock",
        )
        for item in items
        if item.template_code is not None
    }
    return RuntimeClassificationConfirmationWriter(
        database=None,  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        batch_store=FakeBatchStore(batch),  # type: ignore[arg-type]
        training_version_store=FakeTrainingStore(versions),  # type: ignore[arg-type]
        album_review_imports_path=provenance_path,
    )


def command_for(
    tmp_path: Path,
    *,
    product_code: str,
    human_form: str,
    source_type: str = "employee_import_promotion",
    include_source_type: bool = True,
):
    writer = build_writer(
        tmp_path,
        product_code=product_code,
        human_form=human_form,
        source_type=source_type,
        include_source_type=include_source_type,
    )
    command, _ = writer._build_command(
        product_code=product_code,
        package_version=2,
        request_id=REQUEST_ID,
        local_batch_id=BATCH_ID,
    )
    return command


def test_employee_promotion_forces_information_insufficient_material(tmp_path: Path) -> None:
    command = command_for(tmp_path, product_code="BCNHDIY914", human_form="耳饰")

    assert command.material_label == "信息不足"
    assert command.product_form == "earring"
    assert command.visual_observations == ["可见商品轮廓"]
    assert command.selling_point_candidates == []
    assert command.inventory_status is None


@pytest.mark.parametrize("product_code", ["BCNHDIY933", "BCNHDIY1047"])
def test_employee_unknown_forms_remain_unknown(tmp_path: Path, product_code: str) -> None:
    command = command_for(tmp_path, product_code=product_code, human_form="未知")

    assert command.product_form == "unknown"
    assert command.material_label == "信息不足"


def test_legacy_album_provenance_keeps_trainer_confirmed_values(tmp_path: Path) -> None:
    command = command_for(
        tmp_path,
        product_code="BCNHDIY914",
        human_form="耳饰",
        source_type="album_review_import",
        include_source_type=False,
    )

    assert command.material_label == "绿松石"
    assert command.product_form == "earring"
    assert command.selling_point_candidates == ["高价值收藏承诺"]
    assert command.inventory_status == "in_stock"
