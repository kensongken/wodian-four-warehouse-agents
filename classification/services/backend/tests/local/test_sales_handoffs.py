from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from product_engine.api.local_app import create_local_app
from product_engine.contracts.sales_strategy_v2 import (
    SalesStrategyInputV2,
    canonical_sales_strategy_v2_sha256,
)
from product_engine.repositories.sales_handoffs import (
    PreparedSalesPreview,
    StoredSalesHandoff,
    WarehouseHandoffView,
)
from product_engine.services.runtime_sales_handoffs import RuntimeSalesHandoffFailure

CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")
HANDOFF_ID = UUID("77777777-7777-4777-8777-777777777777")
RECEIPT_ID = UUID("88888888-8888-4888-8888-888888888888")


def preview() -> SalesStrategyInputV2:
    value = SalesStrategyInputV2(
        tenant_id="22222222-2222-4222-8222-222222222222",
        product_code="BCNHDIY914",
        strategy_input_version=1,
        source_material_package_version=1,
        source_classification_id=CLASSIFICATION_ID,
        source_classification_version=1,
        source_classification_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        images=(
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        material={"value": "信息不足", "evidence": "information_insufficient"},
        form={"value": "earring", "evidence": "human_confirmed"},
        unverified_items=("材质待人工确认",),
        created_at=datetime(2026, 9, 20, 8, tzinfo=UTC),
        content_sha256="0" * 64,
    )
    return value.model_copy(update={"content_sha256": canonical_sales_strategy_v2_sha256(value)})


def handoff(*, accepted: bool = False) -> WarehouseHandoffView:
    value = preview()
    return WarehouseHandoffView(
        handoff_id=HANDOFF_ID,
        product_code=value.product_code,
        target_warehouse="sales",
        payload_version=1,
        payload_sha256=value.content_sha256,
        idempotency_key=(f"sales:{value.tenant_id}:{value.source_classification_id}"),
        status="accepted" if accepted else "prepared",
        receiver_receipt_id=RECEIPT_ID if accepted else None,
        rejection_code=None,
        prepared_at=value.created_at,
        last_attempted_at=value.created_at if accepted else None,
        settled_at=value.created_at if accepted else None,
    )


class FakeSalesHandoffService:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, UUID]] = []

    def _fail(self) -> None:
        if self.failure:
            raise RuntimeSalesHandoffFailure(self.failure)

    async def prepare(self, classification_id: UUID) -> PreparedSalesPreview:
        self.calls.append(("prepare", classification_id))
        self._fail()
        return PreparedSalesPreview(payload=preview(), handoff=handoff(), created=True)

    async def deliver(self, handoff_id: UUID) -> WarehouseHandoffView:
        self.calls.append(("deliver", handoff_id))
        self._fail()
        return handoff(accepted=True)

    async def read(self, handoff_id: UUID) -> StoredSalesHandoff:
        self.calls.append(("read", handoff_id))
        self._fail()
        return StoredSalesHandoff(payload=preview(), handoff=handoff(accepted=True))


def client(tmp_path, service=None) -> TestClient:
    return TestClient(create_local_app(tmp_path / "data", sales_handoff_service=service))


def test_prepare_returns_preview_and_prepared_handoff(tmp_path) -> None:
    service = FakeSalesHandoffService()

    response = client(tmp_path, service).post(
        f"/local/api/classification-versions/{CLASSIFICATION_ID}/prepare-sales-preview"
    )

    assert response.status_code == 201
    assert response.json()["status"] == "prepared"
    assert response.json()["preview"]["unverified_items"] == ["材质待人工确认"]
    assert service.calls == [("prepare", CLASSIFICATION_ID)]


def test_deliver_only_reports_accepted_with_receiver_receipt(tmp_path) -> None:
    response = client(tmp_path, FakeSalesHandoffService()).post(
        f"/local/api/warehouse-handoffs/{HANDOFF_ID}/deliver"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["receiver_receipt_id"] == str(RECEIPT_ID)


def test_query_restores_the_same_persisted_receipt(tmp_path) -> None:
    response = client(tmp_path, FakeSalesHandoffService()).get(
        f"/local/api/warehouse-handoffs/{HANDOFF_ID}"
    )

    assert response.status_code == 200
    assert response.json()["receiver_receipt_id"] == str(RECEIPT_ID)
    assert response.json()["preview"]["source_classification_id"] == str(CLASSIFICATION_ID)


def test_unconfigured_handoff_service_does_not_claim_delivery(tmp_path) -> None:
    response = client(tmp_path).post(
        f"/local/api/classification-versions/{CLASSIFICATION_ID}/prepare-sales-preview"
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "sales_handoff_unconfigured"


@pytest.mark.parametrize(
    ("error_code", "status_code"),
    [
        ("classification_not_found", 404),
        ("warehouse_handoff_not_found", 404),
        ("classification_not_latest", 409),
        ("classification_ledger_conflict", 409),
        ("sales_payload_hash_mismatch", 409),
        ("sales_permission_boundary_violation", 409),
        ("warehouse_handoff_conflict", 409),
    ],
)
def test_handoff_failures_return_stable_safe_errors(
    tmp_path, error_code: str, status_code: int
) -> None:
    response = client(tmp_path, FakeSalesHandoffService(error_code)).post(
        f"/local/api/classification-versions/{CLASSIFICATION_ID}/prepare-sales-preview"
    )

    assert response.status_code == status_code
    assert response.json()["error_code"] == error_code
    serialized = str(response.json()).casefold()
    assert "traceback" not in serialized
    assert "postgresql" not in serialized
    assert "object_key" not in serialized
    assert str(tmp_path).casefold() not in serialized
