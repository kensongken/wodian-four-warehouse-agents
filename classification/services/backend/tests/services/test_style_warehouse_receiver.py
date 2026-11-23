import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityInputV2,
    StyleWarehouseDecisionReceiptV1,
    StyleWarehouseDeliveryV1,
    canonical_style_eligibility_v2_sha256,
)
from product_engine.repositories.style_handoffs import (
    StyleAuthoritativeGateState,
    StyleTransformAuthorizationView,
)
from product_engine.security.signatures import sign_request
from product_engine.services.style_warehouse_receiver import (
    DELIVERY_PATH,
    SignedStyleDelivery,
    StyleReceiverFailure,
    StyleWarehouseReceiver,
)

NOW_SECONDS = 1_790_000_000
NOW = datetime.fromtimestamp(NOW_SECONDS, UTC)
SECRET = b"style-v2-test-secret"
TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_TENANT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLASSIFICATION_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
AUTHORIZATION_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
ELIGIBILITY_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
HANDOFF_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
IMAGE_SET_SHA256 = "b" * 64


def valid_payload(**updates: object) -> StyleEligibilityInputV2:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "product_code": "BCNHDIY914",
        "eligibility_input_version": 1,
        "source_material_package_version": 1,
        "source_classification_id": CLASSIFICATION_ID,
        "source_classification_version": 1,
        "source_classification_sha256": "a" * 64,
        "source_image_set_sha256": IMAGE_SET_SHA256,
        "main_image": {
            "asset_id": "11111111-1111-4111-8111-111111111111",
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        },
        "images": (
            {
                "asset_id": "11111111-1111-4111-8111-111111111111",
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        "material": {"value": "turquoise", "evidence": "human_confirmed"},
        "form": {"value": "earring", "evidence": "human_confirmed"},
        "transform_authorization_id": AUTHORIZATION_ID,
        "transform_authorization_status": "active",
        "transform_authorization_expires_at": datetime(2026, 10, 1, tzinfo=UTC),
        "created_at": datetime(2026, 9, 20, 8, tzinfo=UTC),
        "content_sha256": "0" * 64,
    }
    values.update(updates)
    value = StyleEligibilityInputV2.model_validate(values)
    return value.model_copy(update={"content_sha256": canonical_style_eligibility_v2_sha256(value)})


def valid_delivery(payload: StyleEligibilityInputV2 | None = None) -> StyleWarehouseDeliveryV1:
    payload = payload or valid_payload()
    return StyleWarehouseDeliveryV1(
        handoff_id=HANDOFF_ID,
        eligibility_input_id=ELIGIBILITY_ID,
        idempotency_key=f"style:{TENANT_ID}:{ELIGIBILITY_ID}",
        payload_sha256=payload.content_sha256,
        payload=payload,
    )


def signed_body(
    body: bytes,
    *,
    tenant_id: UUID = TENANT_ID,
    timestamp: int = NOW_SECONDS,
    secret: bytes = SECRET,
) -> SignedStyleDelivery:
    return SignedStyleDelivery(
        body=body,
        tenant_id=tenant_id,
        timestamp=timestamp,
        signature=sign_request(
            secret,
            timestamp,
            "classification",
            tenant_id,
            "POST",
            DELIVERY_PATH,
            body,
        ),
    )


def signed_delivery(delivery: StyleWarehouseDeliveryV1 | None = None) -> SignedStyleDelivery:
    value = delivery or valid_delivery()
    return signed_body(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def authorization(**updates: object) -> StyleTransformAuthorizationView:
    values: dict[str, object] = {
        "authorization_id": AUTHORIZATION_ID,
        "authorization_request_id": uuid4(),
        "tenant_id": TENANT_ID,
        "classification_id": CLASSIFICATION_ID,
        "source_image_set_sha256": IMAGE_SET_SHA256,
        "scope": "local_style_acceptance_only",
        "expires_at": datetime(2026, 10, 1, tzinfo=UTC),
        "authorized_at": datetime(2026, 9, 20, tzinfo=UTC),
        "revoked_at": None,
    }
    values.update(updates)
    return StyleTransformAuthorizationView(**values)  # type: ignore[arg-type]


def authority(**updates: object) -> StyleAuthoritativeGateState:
    values: dict[str, object] = {
        "classification_id": CLASSIFICATION_ID,
        "classification_version": 1,
        "classification_sha256": "a" * 64,
        "source_image_set_sha256": IMAGE_SET_SHA256,
        "material_evidence": "human_confirmed",
        "authorization": authorization(),
    }
    values.update(updates)
    return StyleAuthoritativeGateState(**values)  # type: ignore[arg-type]


class FakeRepository:
    def __init__(self, state: StyleAuthoritativeGateState | None = None) -> None:
        self.state = state or authority()
        self.receipts: dict[str, tuple[str, StyleWarehouseDecisionReceiptV1]] = {}

    async def authoritative_gate_state(self, tenant_id, delivery):
        return self.state

    async def persist_decision(self, tenant_id, delivery, *, decision, rejection_code):
        existing = self.receipts.get(delivery.idempotency_key)
        if existing is not None:
            digest, receipt = existing
            if digest != delivery.payload_sha256:
                raise ValueError("style_handoff_conflict")
            return receipt
        receipt = StyleWarehouseDecisionReceiptV1(
            decision_receipt_id=uuid4(),
            handoff_id=delivery.handoff_id,
            checked_payload_sha256=delivery.payload_sha256,
            decision=decision,
            rejection_code=rejection_code,
            generation_job_id=None,
            decided_at=NOW,
        )
        self.receipts[delivery.idempotency_key] = (delivery.payload_sha256, receipt)
        return receipt


def receiver(
    *, state: StyleAuthoritativeGateState | None = None, repository=None
) -> StyleWarehouseReceiver:
    return StyleWarehouseReceiver(
        repository or FakeRepository(state),
        SECRET,
        now_seconds=lambda: NOW_SECONDS,
        now_datetime=lambda: NOW,
    )


async def test_valid_delivery_is_accepted_only_as_eligibility() -> None:
    receipt = await receiver().receive(signed_delivery())

    assert receipt.decision == "accepted"
    assert receipt.rejection_code is None
    assert receipt.generation_job_id is None


async def test_material_failure_precedes_missing_authorization() -> None:
    state = authority(material_evidence="information_insufficient", authorization=None)
    receipt = await receiver(state=state).receive(signed_delivery())

    assert receipt.decision == "rejected"
    assert receipt.rejection_code == "classification_material_unconfirmed"
    assert receipt.generation_job_id is None


@pytest.mark.parametrize(
    "permission",
    [
        "automatic_generation_allowed",
        "paid_provider_allowed",
        "automatic_publication_allowed",
        "automatic_customer_send_allowed",
    ],
)
async def test_receiver_persists_true_permission_rejection(permission: str) -> None:
    raw = valid_delivery().model_dump(mode="json")
    raw["payload"][permission] = True
    canonical = {
        key: value
        for key, value in raw["payload"].items()
        if key not in {"content_sha256", "eligibility_input_version", "created_at"}
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    raw["payload"]["content_sha256"] = digest
    raw["payload_sha256"] = digest
    body = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()

    receipt = await receiver().receive(signed_body(body))

    assert receipt.rejection_code == "style_permission_boundary_violation"


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (authority(classification_id=uuid4()), "classification_not_latest"),
        (authority(authorization=None), "style_transform_authorization_required"),
        (
            authority(authorization=authorization(expires_at=datetime(2026, 9, 1, tzinfo=UTC))),
            "style_transform_authorization_expired",
        ),
        (
            authority(authorization=authorization(revoked_at=datetime(2026, 9, 19, tzinfo=UTC))),
            "style_transform_authorization_revoked",
        ),
        (authority(source_image_set_sha256="d" * 64), "style_image_set_mismatch"),
    ],
)
async def test_business_gate_failures_are_durable_rejections(state, code) -> None:
    receipt = await receiver(state=state).receive(signed_delivery())

    assert receipt.decision == "rejected"
    assert receipt.rejection_code == code
    assert receipt.generation_job_id is None


async def test_replay_returns_original_rejection_receipt() -> None:
    repository = FakeRepository(
        authority(material_evidence="information_insufficient", authorization=None)
    )
    service = receiver(repository=repository)

    first = await service.receive(signed_delivery())
    second = await service.receive(signed_delivery())

    assert second.decision_receipt_id == first.decision_receipt_id
    assert len(repository.receipts) == 1


@pytest.mark.parametrize(
    ("signed_input", "code"),
    [
        (replace(signed_delivery(), signature="0" * 64), "invalid_style_warehouse_signature"),
        (
            signed_body(signed_delivery().body, tenant_id=OTHER_TENANT_ID),
            "style_tenant_mismatch",
        ),
    ],
)
async def test_invalid_transport_boundaries_raise(signed_input, code) -> None:
    with pytest.raises(StyleReceiverFailure, match=code):
        await receiver().receive(signed_input)


async def test_unsupported_contract_and_changed_digest_raise() -> None:
    raw = valid_delivery().model_dump(mode="json")
    raw["payload"]["contract_version"] = "3-style-eligibility-input"
    body = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(StyleReceiverFailure, match="unsupported_style_contract_version"):
        await receiver().receive(signed_body(body))

    raw = valid_delivery().model_dump(mode="json")
    raw["payload"]["content_sha256"] = "f" * 64
    raw["payload_sha256"] = "f" * 64
    body = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(StyleReceiverFailure, match="style_payload_hash_mismatch"):
        await receiver().receive(signed_body(body))


async def test_expired_signature_raises() -> None:
    request = replace(signed_delivery(), timestamp=NOW_SECONDS - 301)
    with pytest.raises(StyleReceiverFailure, match="invalid_style_warehouse_signature"):
        await receiver().receive(request)
