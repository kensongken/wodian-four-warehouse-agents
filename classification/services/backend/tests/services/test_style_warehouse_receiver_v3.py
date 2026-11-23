import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from product_engine.contracts.style_warehouse_v3 import (
    StyleEligibilityInputV3,
    StyleWarehouseDeliveryV2,
    canonical_style_eligibility_v3_sha256,
)
from product_engine.repositories.style_handoffs_v3 import StyleAuthoritativeGateStateV3
from product_engine.security.signatures import sign_request
from product_engine.services.style_warehouse_receiver_v3 import (
    DELIVERY_PATH_V3,
    SignedStyleDeliveryV3,
    StyleWarehouseReceiverV3,
)

TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CLASSIFICATION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
EVIDENCE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
AUTHORIZATION_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
NOW = datetime(2026, 9, 21, tzinfo=UTC)
NOW_SECONDS = int(NOW.timestamp())
SECRET = b"style-v3-test-secret"


def payload(*, basis: str = "merchant_declared") -> StyleEligibilityInputV3:
    asset_id = uuid4()
    value = StyleEligibilityInputV3(
        tenant_id=TENANT_ID,
        product_code="BCNHDIY914",
        eligibility_input_version=1,
        source_material_package_version=1,
        source_classification_id=CLASSIFICATION_ID,
        source_classification_version=2,
        source_classification_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        main_image={
            "asset_id": asset_id,
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        },
        images=(
            {
                "asset_id": asset_id,
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        material={
            "value": "绿松石",
            "claim_basis": basis,
            "evidence_record_id": EVIDENCE_ID,
            "evidence_content_sha256": "e" * 64,
        },
        form={"value": "earring", "evidence": "human_confirmed"},
        transform_authorization_id=AUTHORIZATION_ID,
        transform_authorization_status="active",
        transform_authorization_expires_at=datetime(2026, 10, 1, tzinfo=UTC),
        created_at=NOW,
        content_sha256="0" * 64,
    )
    return value.model_copy(update={"content_sha256": canonical_style_eligibility_v3_sha256(value)})


def delivery(value: StyleEligibilityInputV3 | None = None) -> StyleWarehouseDeliveryV2:
    value = value or payload()
    return StyleWarehouseDeliveryV2(
        handoff_id=uuid4(),
        idempotency_key=f"style-v3:{TENANT_ID}:{CLASSIFICATION_ID}",
        payload_sha256=value.content_sha256,
        payload=value,
    )


def state(**updates: object) -> StyleAuthoritativeGateStateV3:
    values = {
        "classification_id": CLASSIFICATION_ID,
        "classification_version": 2,
        "classification_sha256": "a" * 64,
        "source_image_set_sha256": "b" * 64,
        "material_claim_basis": "merchant_declared",
        "material_evidence_id": EVIDENCE_ID,
        "material_evidence_content_sha256": "e" * 64,
        "material_evidence_active": True,
        "authorization_id": AUTHORIZATION_ID,
        "authorization_scope": "local_style_acceptance_only",
        "authorization_expires_at": datetime(2026, 10, 1, tzinfo=UTC),
        "authorization_revoked_at": None,
        "authorization_image_set_sha256": "b" * 64,
    }
    values.update(updates)
    return StyleAuthoritativeGateStateV3(**values)  # type: ignore[arg-type]


class Repository:
    def __init__(self, authority: StyleAuthoritativeGateStateV3) -> None:
        self.authority = authority
        self.receipts = {}

    async def authoritative_gate_state(self, tenant_id, value):
        return self.authority

    async def persist_decision(self, tenant_id, value, *, decision, rejection_code):
        from product_engine.contracts.style_warehouse_v3 import StyleWarehouseDecisionReceiptV2

        receipt = self.receipts.get(value.idempotency_key)
        if receipt is None:
            receipt = StyleWarehouseDecisionReceiptV2(
                decision_receipt_id=uuid4(),
                handoff_id=value.handoff_id,
                checked_payload_sha256=value.payload_sha256,
                decision=decision,
                rejection_code=rejection_code,
                generation_job_id=None,
                decided_at=NOW,
            )
            self.receipts[value.idempotency_key] = receipt
        return receipt


def signed(value: StyleWarehouseDeliveryV2) -> SignedStyleDeliveryV3:
    body = json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    return SignedStyleDeliveryV3(
        body=body,
        tenant_id=TENANT_ID,
        timestamp=NOW_SECONDS,
        signature=sign_request(
            SECRET, NOW_SECONDS, "classification", TENANT_ID, "POST", DELIVERY_PATH_V3, body
        ),
    )


@pytest.mark.parametrize("basis", ["document_supported", "merchant_declared"])
async def test_supported_claim_basis_continues_to_authorization_gate(basis: str) -> None:
    value = payload(basis=basis)
    repository = Repository(state(material_claim_basis=basis, authorization_id=None))
    receipt = await StyleWarehouseReceiverV3(
        repository, SECRET, now_seconds=lambda: NOW_SECONDS, now_datetime=lambda: NOW
    ).receive(signed(delivery(value)))
    assert receipt.rejection_code == "style_transform_authorization_required"


async def test_revoked_after_prepare_is_rejected_at_receive_time() -> None:
    repository = Repository(state(material_evidence_active=False))
    receipt = await StyleWarehouseReceiverV3(
        repository, SECRET, now_seconds=lambda: NOW_SECONDS, now_datetime=lambda: NOW
    ).receive(signed(delivery()))
    assert receipt.rejection_code == "material_evidence_invalid"
    assert receipt.generation_job_id is None


async def test_legacy_confirmation_without_provenance_is_rejected() -> None:
    repository = Repository(state(material_claim_basis=None, material_evidence_id=None))
    receipt = await StyleWarehouseReceiverV3(
        repository, SECRET, now_seconds=lambda: NOW_SECONDS, now_datetime=lambda: NOW
    ).receive(signed(delivery()))
    assert receipt.rejection_code == "material_evidence_provenance_missing"


async def test_mismatched_authorization_is_reported_as_required() -> None:
    repository = Repository(state(authorization_id=uuid4()))
    receipt = await StyleWarehouseReceiverV3(
        repository,
        SECRET,
        now_seconds=lambda: NOW_SECONDS,
        now_datetime=lambda: NOW,
    ).receive(signed(delivery()))

    assert receipt.rejection_code == "style_transform_authorization_required"
