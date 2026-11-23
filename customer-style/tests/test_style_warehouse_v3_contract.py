import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from opc_style.contracts.style_warehouse_v3 import (
    StyleEligibilityInputV4,
    StyleWarehouseDecisionReceiptV3,
    StyleWarehouseDeliveryV3,
    canonical_style_eligibility_v4_sha256,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
CLASSIFICATION_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
EVIDENCE_ID = UUID("55555555-5555-4555-8555-555555555555")
AUTHORIZATION_ID = UUID("66666666-6666-4666-8666-666666666666")
HANDOFF_ID = UUID("77777777-7777-4777-8777-777777777777")
NOW = datetime(2026, 9, 28, 2, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def eligibility(**updates: object) -> StyleEligibilityInputV4:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "product_code": "SYNTHETIC-001",
        "eligibility_input_version": 1,
        "source_material_package_version": 2,
        "source_classification_id": CLASSIFICATION_ID,
        "source_classification_version": 2,
        "source_classification_sha256": "a" * 64,
        "source_image_set_sha256": "b" * 64,
        "main_image": {
            "asset_id": ASSET_ID,
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        },
        "images": [
            {
                "asset_id": ASSET_ID,
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            }
        ],
        "material": {
            "value": "synthetic-material",
            "claim_basis": "merchant_declared",
            "evidence_record_id": EVIDENCE_ID,
            "evidence_content_sha256": "d" * 64,
        },
        "form": {"value": "bracelet", "evidence": "human_confirmed"},
        "transform_authorization_id": AUTHORIZATION_ID,
        "transform_authorization_status": "active",
        "transform_authorization_scope": "local_style_acceptance_only",
        "transform_authorization_image_set_sha256": "b" * 64,
        "transform_authorization_expires_at": NOW + timedelta(hours=1),
        "transform_authorization_revoked_at": None,
        "created_at": NOW,
        "content_sha256": "0" * 64,
        "automatic_generation_allowed": False,
        "paid_provider_allowed": False,
        "automatic_publication_allowed": False,
        "automatic_customer_send_allowed": False,
    }
    values.update(updates)
    value = StyleEligibilityInputV4.model_validate(values)
    return value.model_copy(
        update={"content_sha256": canonical_style_eligibility_v4_sha256(value)}
    )


def delivery(value: StyleEligibilityInputV4 | None = None) -> StyleWarehouseDeliveryV3:
    value = value or eligibility()
    return StyleWarehouseDeliveryV3(
        handoff_id=HANDOFF_ID,
        idempotency_key=f"style-v4:{TENANT_ID}:{CLASSIFICATION_ID}",
        payload_sha256=value.content_sha256,
        payload=value,
    )


def test_contract_accepts_complete_local_only_delivery() -> None:
    value = delivery()

    assert value.schema_version == "style-warehouse-delivery-v3"
    assert value.payload.contract_version == "4-style-eligibility-input"
    assert value.payload.transform_authorization_scope == "local_style_acceptance_only"


def test_committed_golden_fixture_and_schema_match_model() -> None:
    fixture = StyleWarehouseDeliveryV3.model_validate_json(
        (ROOT / "fixtures/style-warehouse-delivery.v3.synthetic.json").read_bytes()
    )
    schema = json.loads(
        (ROOT / "contracts/style-warehouse-delivery.v3.schema.json").read_text()
    )

    assert fixture == delivery()
    assert schema == StyleWarehouseDeliveryV3.model_json_schema()


def test_digest_ignores_transport_version_and_created_time_but_binds_authority() -> None:
    original = eligibility()
    replay = original.model_copy(
        update={"eligibility_input_version": 9, "created_at": NOW + timedelta(minutes=1)}
    )
    changed_scope = original.model_copy(
        update={"transform_authorization_scope": "wrong"}
    )
    changed_images = original.model_copy(
        update={"transform_authorization_image_set_sha256": "e" * 64}
    )

    assert canonical_style_eligibility_v4_sha256(replay) == original.content_sha256
    assert canonical_style_eligibility_v4_sha256(changed_scope) != original.content_sha256
    assert canonical_style_eligibility_v4_sha256(changed_images) != original.content_sha256


def test_rejects_main_image_outside_images() -> None:
    with pytest.raises(ValidationError):
        eligibility(main_image={
            "asset_id": UUID("88888888-8888-4888-8888-888888888888"),
            "sha256": "e" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        })


def test_rejects_automatic_permission_escalation() -> None:
    with pytest.raises(ValidationError):
        eligibility(automatic_generation_allowed=True)


@pytest.mark.parametrize(
    "field",
    [
        "transform_authorization_id",
        "transform_authorization_scope",
        "transform_authorization_image_set_sha256",
        "transform_authorization_expires_at",
    ],
)
def test_active_authorization_requires_complete_signed_snapshot(field: str) -> None:
    with pytest.raises(ValidationError):
        eligibility(**{field: None})


def test_rejects_idempotency_key_not_bound_to_source() -> None:
    with pytest.raises(ValidationError):
        StyleWarehouseDeliveryV3(
            handoff_id=HANDOFF_ID,
            idempotency_key="style-v4:wrong:source",
            payload_sha256=eligibility().content_sha256,
            payload=eligibility(),
        )


def test_strictly_rejects_unknown_fields() -> None:
    raw = delivery().model_dump(mode="json")
    raw["unexpected"] = True
    with pytest.raises(ValidationError):
        StyleWarehouseDeliveryV3.model_validate(raw)


def test_decision_receipt_separates_transport_disposition() -> None:
    receipt = StyleWarehouseDecisionReceiptV3(
        decision_receipt_id=UUID("99999999-9999-4999-8999-999999999999"),
        handoff_id=HANDOFF_ID,
        checked_payload_sha256=eligibility().content_sha256,
        decision="accepted",
        rejection_code=None,
        generation_job_id=None,
        decided_at=NOW,
        disposition="duplicate",
    )

    assert json.loads(receipt.model_dump_json())["disposition"] == "duplicate"
