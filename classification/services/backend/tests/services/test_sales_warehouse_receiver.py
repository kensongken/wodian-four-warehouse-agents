import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from product_engine.contracts.sales_strategy_v2 import (
    SalesStrategyInputV2,
    SalesWarehouseDeliveryV1,
    SalesWarehouseReceiptV1,
    canonical_sales_strategy_v2_sha256,
)
from product_engine.security.signatures import sign_request
from product_engine.services.sales_warehouse_receiver import (
    SalesReceiverFailure,
    SalesWarehouseReceiver,
    SignedSalesDelivery,
)

NOW = 1_789_891_200
SECRET = b"sales-v2-test-secret"
PATH = "/internal/sales-warehouse/handoffs"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_TENANT_ID = UUID("99999999-9999-4999-8999-999999999999")
CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")
HANDOFF_ID = UUID("77777777-7777-4777-8777-777777777777")


def valid_payload(**updates: object) -> SalesStrategyInputV2:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "product_code": "BCNHDIY914",
        "strategy_input_version": 1,
        "source_material_package_version": 1,
        "source_classification_id": CLASSIFICATION_ID,
        "source_classification_version": 1,
        "source_classification_sha256": "a" * 64,
        "source_image_set_sha256": "b" * 64,
        "images": (
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        "material": {
            "value": "信息不足",
            "evidence": "information_insufficient",
        },
        "form": {"value": "earring", "evidence": "human_confirmed"},
        "unverified_items": ("材质待人工确认",),
        "created_at": datetime(2026, 9, 20, 8, tzinfo=UTC),
        "content_sha256": "0" * 64,
    }
    values.update(updates)
    value = SalesStrategyInputV2.model_validate(values)
    return value.model_copy(update={"content_sha256": canonical_sales_strategy_v2_sha256(value)})


def valid_delivery() -> SalesWarehouseDeliveryV1:
    payload = valid_payload()
    return SalesWarehouseDeliveryV1(
        handoff_id=HANDOFF_ID,
        idempotency_key=f"sales:{TENANT_ID}:{CLASSIFICATION_ID}",
        payload_sha256=payload.content_sha256,
        payload=payload,
    )


def signed_body(
    body: bytes,
    *,
    tenant_id: UUID = TENANT_ID,
    timestamp: int = NOW,
    secret: bytes = SECRET,
) -> SignedSalesDelivery:
    return SignedSalesDelivery(
        body=body,
        tenant_id=tenant_id,
        timestamp=timestamp,
        signature=sign_request(
            secret,
            timestamp,
            "classification",
            tenant_id,
            "POST",
            PATH,
            body,
        ),
    )


def signed_delivery(delivery: SalesWarehouseDeliveryV1) -> SignedSalesDelivery:
    return signed_body(
        json.dumps(
            delivery.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def signed_mutation(mutator) -> SignedSalesDelivery:
    raw = valid_delivery().model_dump(mode="json")
    mutator(raw)
    body = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return signed_body(body)


class FakeReceiptRepository:
    def __init__(self) -> None:
        self.receipts: dict[str, tuple[str, SalesWarehouseReceiptV1]] = {}

    async def accept(self, tenant_id: UUID, delivery: SalesWarehouseDeliveryV1):
        existing = self.receipts.get(delivery.idempotency_key)
        if existing is not None:
            digest, receipt = existing
            if digest != delivery.payload_sha256:
                raise SalesReceiverFailure("warehouse_handoff_conflict")
            return receipt
        receipt = SalesWarehouseReceiptV1(
            receiver_receipt_id=uuid4(),
            handoff_id=delivery.handoff_id,
            accepted_payload_sha256=delivery.payload_sha256,
            accepted_at=datetime(2026, 9, 20, 8, 1, tzinfo=UTC),
        )
        self.receipts[delivery.idempotency_key] = (
            delivery.payload_sha256,
            receipt,
        )
        return receipt


def receiver(repository: FakeReceiptRepository | None = None) -> SalesWarehouseReceiver:
    return SalesWarehouseReceiver(
        repository or FakeReceiptRepository(),
        SECRET,
        now=lambda: NOW,
    )


@pytest.mark.parametrize(
    "permission",
    [
        "automatic_customer_contact_allowed",
        "automatic_publication_allowed",
        "automatic_wechat_send_allowed",
        "automatic_quote_allowed",
    ],
)
async def test_receiver_rejects_any_automatic_permission(permission: str) -> None:
    request = signed_mutation(lambda raw: raw["payload"].__setitem__(permission, True))

    with pytest.raises(SalesReceiverFailure, match="sales_permission_boundary_violation"):
        await receiver().receive(request)


async def test_receiver_replay_returns_original_receipt() -> None:
    repository = FakeReceiptRepository()
    service = receiver(repository)

    first = await service.receive(signed_delivery(valid_delivery()))
    second = await service.receive(signed_delivery(valid_delivery()))

    assert second.receiver_receipt_id == first.receiver_receipt_id
    assert len(repository.receipts) == 1


async def test_same_idempotency_key_with_changed_payload_is_rejected() -> None:
    repository = FakeReceiptRepository()
    service = receiver(repository)
    await service.receive(signed_delivery(valid_delivery()))
    changed_payload = valid_payload(source_classification_sha256="d" * 64)
    changed = valid_delivery().model_copy(
        update={
            "payload": changed_payload,
            "payload_sha256": changed_payload.content_sha256,
        }
    )

    with pytest.raises(SalesReceiverFailure, match="warehouse_handoff_conflict"):
        await service.receive(signed_delivery(changed))


@pytest.mark.parametrize(
    ("signed_input", "error_code"),
    [
        (
            replace(signed_delivery(valid_delivery()), signature="0" * 64),
            "invalid_warehouse_signature",
        ),
        (
            signed_body(
                signed_delivery(valid_delivery()).body,
                tenant_id=OTHER_TENANT_ID,
            ),
            "sales_tenant_mismatch",
        ),
        (
            signed_mutation(
                lambda raw: raw["payload"].__setitem__("contract_version", "3-sales-strategy-input")
            ),
            "unsupported_sales_contract_version",
        ),
        (
            signed_mutation(lambda raw: raw["payload"].__setitem__("content_sha256", "f" * 64)),
            "sales_payload_hash_mismatch",
        ),
        (
            signed_mutation(lambda raw: raw["payload"].__setitem__("status", "sales_ready")),
            "sales_permission_boundary_violation",
        ),
    ],
)
async def test_receiver_rejects_invalid_boundary(
    signed_input: SignedSalesDelivery, error_code: str
) -> None:
    with pytest.raises(SalesReceiverFailure, match=error_code):
        await receiver().receive(signed_input)


async def test_receiver_rejects_expired_signature() -> None:
    delivery = valid_delivery()
    request = replace(signed_delivery(delivery), timestamp=NOW - 301)

    with pytest.raises(SalesReceiverFailure, match="invalid_warehouse_signature"):
        await receiver().receive(request)
