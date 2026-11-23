from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from product_engine.api.local_app import create_local_app
from product_engine.contracts.employee_formal_handoff import (
    EmployeeFormalHandoffReceipt,
    EmployeePromotionAssetReceipt,
    EmployeePromotionProductReceipt,
)
from product_engine.services.employee_batch_promotions import EmployeeBatchPromotionFailure
from product_engine.services.employee_handoff_receipts import EmployeeHandoffReceiptStore
from product_engine.services.local_upload_batches import LocalUploadBatchStore
from product_engine.services.template_registry import TemplateRecord
from product_engine.services.training_label_versions import (
    TrainingLabelInput,
    TrainingLabelVersionStore,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_ID = UUID("22222222-2222-4222-8222-222222222222")
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
PRODUCT_CODE = "BCNHDIY914"


def created_receipt(batch_id: str = "BATCH-TEST") -> EmployeeFormalHandoffReceipt:
    return EmployeeFormalHandoffReceipt(
        request_id=REQUEST_ID,
        tenant_id=TENANT_ID,
        batch_id=batch_id,
        content_sha256="a" * 64,
        products=(
            EmployeePromotionProductReceipt(
                product_code=PRODUCT_CODE,
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


class FakeEmployeeBatchPromotionService:
    def __init__(
        self,
        receipt: EmployeeFormalHandoffReceipt | None = None,
        *,
        failure: str | None = None,
    ) -> None:
        self.receipt = receipt
        self.failure = failure
        self.calls: list[tuple[str, UUID]] = []

    async def promote(self, batch_id: str, request_id: UUID) -> EmployeeFormalHandoffReceipt:
        self.calls.append((batch_id, request_id))
        if self.failure is not None:
            raise EmployeeBatchPromotionFailure(self.failure)
        assert self.receipt is not None
        return self.receipt

    def latest(self, batch_id: str) -> EmployeeFormalHandoffReceipt | None:
        if self.receipt is not None and self.receipt.batch_id == batch_id:
            return self.receipt
        return None


def test_create_formal_handoff_accepts_only_request_id(tmp_path: Path) -> None:
    receipt = created_receipt()
    service = FakeEmployeeBatchPromotionService(receipt)
    client = TestClient(create_local_app(tmp_path, employee_batch_promotion_service=service))

    response = client.post(
        f"/local/api/upload-batches/{receipt.batch_id}/formal-handoffs",
        json={"request_id": str(REQUEST_ID)},
    )

    assert response.status_code == 201
    assert response.json()["downstream_status"] == "not_sent"
    assert response.json()["products"][0]["package_version"] == 2
    assert service.calls == [(receipt.batch_id, REQUEST_ID)]


def test_create_formal_handoff_rejects_browser_identity_fields(tmp_path: Path) -> None:
    receipt = created_receipt()
    client = TestClient(
        create_local_app(
            tmp_path,
            employee_batch_promotion_service=FakeEmployeeBatchPromotionService(receipt),
        )
    )
    response = client.post(
        f"/local/api/upload-batches/{receipt.batch_id}/formal-handoffs",
        json={"request_id": str(REQUEST_ID), "tenant_id": str(TENANT_ID)},
    )
    assert response.status_code == 422


def test_latest_formal_handoff_returns_persisted_receipt_only(tmp_path: Path) -> None:
    receipt = created_receipt()
    EmployeeHandoffReceiptStore(tmp_path / "employee-formal-handoffs.json").save(receipt)
    client = TestClient(create_local_app(tmp_path))

    found = client.get(f"/local/api/upload-batches/{receipt.batch_id}/formal-handoffs/latest")
    missing = client.get("/local/api/upload-batches/BATCH-MISSING/formal-handoffs/latest")

    assert found.status_code == 200
    assert found.json()["products"][0]["event_id"] == str(EVENT_ID)
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "employee_handoff_not_found"


def test_unconfigured_promotion_returns_404(tmp_path: Path) -> None:
    response = TestClient(create_local_app(tmp_path)).post(
        "/local/api/upload-batches/BATCH-TEST/formal-handoffs",
        json={"request_id": str(REQUEST_ID)},
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "employee_handoff_unconfigured"


@pytest.mark.parametrize(
    ("error_code", "status_code"),
    [
        ("employee_batch_review_incomplete", 409),
        ("employee_batch_already_changed", 409),
        ("employee_handoff_request_conflict", 409),
        ("employee_handoff_ledger_conflict", 409),
        ("employee_product_main_image_invalid", 422),
        ("employee_product_form_unconfirmed", 422),
        ("employee_source_image_unreadable", 422),
        ("employee_handoff_storage_unavailable", 503),
    ],
)
def test_promotion_errors_have_stable_http_status(
    tmp_path: Path,
    error_code: str,
    status_code: int,
) -> None:
    client = TestClient(
        create_local_app(
            tmp_path / error_code,
            employee_batch_promotion_service=FakeEmployeeBatchPromotionService(failure=error_code),
        )
    )
    response = client.post(
        "/local/api/upload-batches/BATCH-TEST/formal-handoffs",
        json={"request_id": str(REQUEST_ID)},
    )
    assert response.status_code == status_code
    assert response.json()["error_code"] == error_code
    assert str(tmp_path) not in response.text


def seed_reviewed_batch(data_root: Path) -> tuple[str, UUID, str]:
    store = LocalUploadBatchStore(data_root / "upload-batches.json")
    batch = store.create(expected_count=1)
    record = TemplateRecord(
        id=uuid4(),
        tenant_id=TENANT_ID,
        template_code="EMPLOYEE-914-1",
        version=1,
        category="product",
        image_type="source",
        object_key="local/914-1.jpg",
        sha256="c" * 64,
        mime_type="image/jpeg",
        byte_size=100,
    )
    item = store.add_image(
        batch.batch_id,
        0,
        "BCNHDIY914.jpg",
        record,
        product_code=PRODUCT_CODE,
        product_sequence=1,
        image_role="main",
        source_system="employee_workbook",
    )
    store.mark_reviewed(batch.batch_id, item.item_id)
    return batch.batch_id, item.item_id, record.template_code


def test_promoted_batch_rejects_every_structural_mutation(tmp_path: Path) -> None:
    batch_id, item_id, _ = seed_reviewed_batch(tmp_path)
    target_batch = LocalUploadBatchStore(tmp_path / "upload-batches.json").create(expected_count=1)
    EmployeeHandoffReceiptStore(tmp_path / "employee-formal-handoffs.json").save(
        created_receipt(batch_id)
    )
    client = TestClient(
        create_local_app(
            tmp_path,
            style_test_bridge_enabled=True,
            style_test_catalog={
                "test-product-target": {
                    "customer_test_id": "test-customer-target",
                    "product_id": "test-product-target",
                    "product_category": "turquoise",
                    "product_form": "bracelet",
                }
            },
        )
    )
    before = client.get(f"/local/api/upload-batches/{batch_id}").json()
    requests = (
        client.delete(f"/local/api/upload-batches/{batch_id}/items/{item_id}"),
        client.post(
            f"/local/api/upload-batches/{batch_id}/items/{item_id}/move",
            json={"to_position": 2},
        ),
        client.post(
            f"/local/api/upload-batches/{batch_id}/items/{item_id}/move-to-batch",
            json={
                "target_batch_id": target_batch.batch_id,
                "target_product_id": "test-product-target",
            },
        ),
        client.post(
            f"/local/api/upload-batches/{batch_id}/images",
            data={"position": "0"},
            files={"file": ("replacement.jpg", b"replacement", "image/jpeg")},
        ),
        client.post(
            f"/local/api/upload-batches/{batch_id}/workbook",
            files={
                "file": (
                    "products.xlsx",
                    b"not-read-because-batch-is-sealed",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        ),
    )

    for response in requests:
        assert response.status_code == 409
        assert response.json()["error_code"] == "employee_batch_promoted_immutable"
    assert client.get(f"/local/api/upload-batches/{batch_id}").json() == before


def test_training_text_remains_editable_without_changing_package_version(tmp_path: Path) -> None:
    batch_id, _, template_code = seed_reviewed_batch(tmp_path)
    versions = TrainingLabelVersionStore(
        tmp_path / "training-label-versions.jsonl",
        tmp_path / "training-labels.jsonl",
    )
    versions.register_initial(
        template_code,
        TrainingLabelInput(
            sha256="c" * 64,
            material_label="信息不足",
            product_form="耳饰",
            visual_observations=["可见耳饰形态"],
            selling_points=["待人工补充"],
        ),
    )
    receipt = created_receipt(batch_id)
    EmployeeHandoffReceiptStore(tmp_path / "employee-formal-handoffs.json").save(receipt)
    client = TestClient(create_local_app(tmp_path))

    response = client.post(
        f"/local/api/training-results/{template_code}/versions",
        json={
            "base_version": 1,
            "sha256": "c" * 64,
            "material_label": "信息不足",
            "product_form": "耳饰",
            "visual_observations": ["耳饰轮廓清晰"],
            "selling_points": ["待人工补充"],
            "specification_note": "",
            "quality_note": "",
            "defect_note": "",
            "price_range": None,
            "service_rules": [],
        },
    )

    assert response.status_code == 201
    latest = client.get(f"/local/api/upload-batches/{batch_id}/formal-handoffs/latest")
    assert latest.json()["products"][0]["package_version"] == 2
