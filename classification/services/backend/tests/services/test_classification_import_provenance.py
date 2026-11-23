import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from product_engine.contracts.employee_formal_handoff import (
    EmployeeFormalHandoffReceipt,
    EmployeePromotionAssetReceipt,
    EmployeePromotionProductReceipt,
)
from product_engine.services.classification_import_provenance import (
    ClassificationImportProvenanceFailure,
    ClassificationImportProvenanceStore,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("22222222-2222-4222-8222-222222222222")
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
BATCH_ID = "BATCH-20260921035620-3A284AA4"


def employee_receipt() -> EmployeeFormalHandoffReceipt:
    return EmployeeFormalHandoffReceipt(
        request_id=REQUEST_ID,
        tenant_id=TENANT_ID,
        batch_id=BATCH_ID,
        content_sha256="a" * 64,
        products=(
            EmployeePromotionProductReceipt(
                product_code="BCNHDIY914",
                package_version=2,
                event_id=EVENT_ID,
                asset_count=1,
                content_sha256="b" * 64,
                assets=(
                    EmployeePromotionAssetReceipt(
                        item_id=ASSET_ID,
                        asset_id=ASSET_ID,
                        object_key="tenants/t/employee-handoffs/b/p/a",
                        sha256="c" * 64,
                        mime_type="image/jpeg",
                        role="main_candidate",
                        position=0,
                    ),
                ),
            ),
        ),
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
        disposition="created",
    )


def test_employee_receipt_writes_typed_provenance_without_local_paths(tmp_path: Path) -> None:
    path = tmp_path / "album-review-imports.json"
    store = ClassificationImportProvenanceStore(path)

    store.save_employee_receipt(employee_receipt())

    record = store.get("BCNHDIY914", 2)
    assert record is not None
    assert record.source_type == "employee_import_promotion"
    assert record.batch_id == BATCH_ID
    assert record.assets[0].item_id == ASSET_ID
    assert record.assets[0].sha256 == "c" * 64
    assert "upload-batches" not in path.read_text(encoding="utf-8")


def test_old_album_dict_without_source_type_loads_compatibly(tmp_path: Path) -> None:
    path = tmp_path / "album-review-imports.json"
    path.write_text(
        json.dumps(
            {
                "BCNHDIY914:v1": {
                    "batch_id": "BATCH-ALBUM",
                    "assets": [
                        {
                            "item_id": str(ASSET_ID),
                            "asset_id": str(ASSET_ID),
                            "position": 0,
                            "role": "main_candidate",
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    record = ClassificationImportProvenanceStore(path).get("BCNHDIY914", 1)

    assert record is not None
    assert record.source_type == "album_review_import"
    assert record.assets[0].sha256 is None


def test_legacy_string_is_album_only_and_preserved_on_write(tmp_path: Path) -> None:
    path = tmp_path / "album-review-imports.json"
    path.write_text(json.dumps({"OLD:v1": "BATCH-OLD"}), encoding="utf-8")
    store = ClassificationImportProvenanceStore(path)

    legacy = store.get("OLD", 1)
    store.save_employee_receipt(employee_receipt())

    assert legacy is not None
    assert legacy.source_type == "album_review_import"
    assert legacy.assets == ()
    assert json.loads(path.read_text(encoding="utf-8"))["OLD:v1"] == "BATCH-OLD"


@pytest.mark.parametrize("content", ["not-json", "[]", '{"broken": true}'])
def test_malformed_or_invalid_provenance_fails_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "album-review-imports.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(
        ClassificationImportProvenanceFailure,
        match="classification_import_provenance_invalid",
    ):
        ClassificationImportProvenanceStore(path).get("BCNHDIY914", 1)
