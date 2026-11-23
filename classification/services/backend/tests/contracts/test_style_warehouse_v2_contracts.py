from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityInputV2,
    StyleTransformAuthorizationCommand,
    StyleWarehouseDecisionReceiptV1,
    StyleWarehouseDeliveryV1,
    canonical_style_eligibility_v2_sha256,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")
ELIGIBILITY_INPUT_ID = UUID("55555555-5555-4555-8555-555555555555")
HANDOFF_ID = UUID("77777777-7777-4777-8777-777777777777")
RECEIPT_ID = UUID("88888888-8888-4888-8888-888888888888")
AUTHORIZATION_ID = UUID("99999999-9999-4999-8999-999999999999")
AUTHORIZATION_REQUEST_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
MAIN_ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")


def valid_eligibility(**updates: object) -> StyleEligibilityInputV2:
    raw: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "product_code": "BCNHDIY914",
        "eligibility_input_version": 1,
        "source_material_package_version": 1,
        "source_classification_id": CLASSIFICATION_ID,
        "source_classification_version": 1,
        "source_classification_sha256": "a" * 64,
        "source_image_set_sha256": "b" * 64,
        "main_image": {
            "asset_id": MAIN_ASSET_ID,
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        },
        "images": (
            {
                "asset_id": MAIN_ASSET_ID,
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        "material": {"value": "信息不足", "evidence": "information_insufficient"},
        "form": {"value": "earring", "evidence": "human_confirmed"},
        "transform_authorization_id": None,
        "transform_authorization_status": "none",
        "transform_authorization_expires_at": None,
        "transform_authorization_revoked_at": None,
        "created_at": datetime(2026, 9, 20, 8, tzinfo=UTC),
        "content_sha256": "0" * 64,
    }
    raw.update(updates)
    value = StyleEligibilityInputV2.model_validate(raw)
    return value.model_copy(update={"content_sha256": canonical_style_eligibility_v2_sha256(value)})


def valid_delivery() -> StyleWarehouseDeliveryV1:
    payload = valid_eligibility()
    return StyleWarehouseDeliveryV1(
        handoff_id=HANDOFF_ID,
        eligibility_input_id=ELIGIBILITY_INPUT_ID,
        idempotency_key=f"style:{TENANT_ID}:{ELIGIBILITY_INPUT_ID}",
        payload_sha256=payload.content_sha256,
        payload=payload,
    )


def test_digest_is_stable_and_main_image_belongs_to_images() -> None:
    value = valid_eligibility()

    assert canonical_style_eligibility_v2_sha256(value) == value.content_sha256
    assert value.main_image.asset_id in {image.asset_id for image in value.images}


def test_main_image_must_match_an_image_entry() -> None:
    with pytest.raises(ValidationError, match="main image must belong to images"):
        valid_eligibility(
            main_image={
                "asset_id": "33333333-3333-4333-8333-333333333333",
                "sha256": "d" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            }
        )


@pytest.mark.parametrize(
    "field",
    [
        "automatic_generation_allowed",
        "paid_provider_allowed",
        "automatic_publication_allowed",
        "automatic_customer_send_allowed",
    ],
)
def test_every_automatic_permission_requires_exact_false(field: str) -> None:
    with pytest.raises(ValidationError, match="automatic permissions must be false"):
        valid_eligibility(**{field: True})


@pytest.mark.parametrize(
    "updates",
    [
        {"transform_authorization_status": "active"},
        {
            "transform_authorization_id": AUTHORIZATION_ID,
            "transform_authorization_status": "none",
            "transform_authorization_expires_at": datetime(2026, 9, 27, tzinfo=UTC),
        },
        {
            "transform_authorization_id": AUTHORIZATION_ID,
            "transform_authorization_status": "active",
            "transform_authorization_expires_at": datetime(2026, 9, 27, tzinfo=UTC),
            "transform_authorization_revoked_at": datetime(2026, 9, 21, tzinfo=UTC),
        },
    ],
)
def test_authorization_snapshot_fields_must_be_consistent(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="authorization snapshot is inconsistent"):
        valid_eligibility(**updates)


def test_active_and_revoked_authorization_snapshots_are_distinct() -> None:
    active = valid_eligibility(
        transform_authorization_id=AUTHORIZATION_ID,
        transform_authorization_status="active",
        transform_authorization_expires_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    revoked = valid_eligibility(
        transform_authorization_id=AUTHORIZATION_ID,
        transform_authorization_status="revoked",
        transform_authorization_expires_at=datetime(2026, 9, 27, tzinfo=UTC),
        transform_authorization_revoked_at=datetime(2026, 9, 21, tzinfo=UTC),
    )

    assert active.content_sha256 != revoked.content_sha256


def test_delivery_key_and_digest_are_bound_to_the_eligibility_input() -> None:
    delivery = valid_delivery()
    assert delivery.payload_sha256 == delivery.payload.content_sha256

    with pytest.raises(ValidationError, match="idempotency key must match eligibility input"):
        valid_delivery().model_copy(
            update={"idempotency_key": f"style:{TENANT_ID}:{HANDOFF_ID}"}
        ).model_validate(
            {
                **valid_delivery().model_dump(),
                "idempotency_key": f"style:{TENANT_ID}:{HANDOFF_ID}",
            }
        )


def test_authorization_command_requires_explicit_request_id_and_expiry() -> None:
    command = StyleTransformAuthorizationCommand(
        authorization_request_id=AUTHORIZATION_REQUEST_ID,
        scope="local_style_acceptance_only",
        expires_at=datetime(2026, 9, 27, 8, tzinfo=UTC),
    )

    assert command.scope == "local_style_acceptance_only"
    with pytest.raises(ValidationError):
        StyleTransformAuthorizationCommand.model_validate(
            {
                "scope": "local_style_acceptance_only",
                "expires_at": "2026-09-27T08:00:00Z",
            }
        )


def test_decision_receipt_requires_rejection_code_only_for_rejection() -> None:
    rejected = StyleWarehouseDecisionReceiptV1(
        decision_receipt_id=RECEIPT_ID,
        handoff_id=HANDOFF_ID,
        checked_payload_sha256="d" * 64,
        decision="rejected",
        rejection_code="classification_material_unconfirmed",
        generation_job_id=None,
        decided_at=datetime(2026, 9, 20, 8, 1, tzinfo=UTC),
    )
    assert rejected.generation_job_id is None

    with pytest.raises(ValidationError, match="rejection code must match decision"):
        rejected.model_copy(update={"decision": "accepted"}).model_validate(
            {**rejected.model_dump(), "decision": "accepted"}
        )
