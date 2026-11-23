import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from opc_style.contracts.mall_style_handoff_v1 import (
    MallProductAssetImportReceiptV1,
    MallStyleAssetRefV1,
    MallStyleGenerationApprovalV1,
    MallStyleGenerationQueueReceiptV1,
    MallStyleHandoffV1,
    MallStyleResultListV1,
    MallStyleSelectionListV1,
    MallStyleSelectionV1,
    MallSubjectConfirmationMetadataV1,
    MallSubjectConfirmationReceiptV1,
    MallSubjectInspectionV1,
    canonical_mall_generation_approval_sha256,
    canonical_mall_payload_sha256,
)

ROOT = Path(__file__).resolve().parents[1]


def handoff_data(*, with_product: bool = False) -> dict[str, object]:
    submission_id = uuid4()
    label = MallStyleAssetRefV1(
        source_asset_id=uuid4(),
        source_content_sha256="a" * 64,
        asset_role="label_photo",
        mime_type="image/jpeg",
        authorized_purposes=("product_metadata_extraction",),
    )
    assets = [label]
    selected = None
    if with_product:
        product = MallStyleAssetRefV1(
            source_asset_id=uuid4(),
            source_content_sha256="b" * 64,
            asset_role="product_photo",
            mime_type="image/png",
            style_product_asset_id=uuid4(),
            authorized_purposes=("style_subject_transform",),
        )
        assets.append(product)
        selected = product.source_asset_id
    return {
        "schema_version": "mall-style-handoff-v1",
        "handoff_id": uuid4(),
        "merchant_id": uuid4(),
        "opc_id": uuid4(),
        "product_entry_submission_id": submission_id,
        "product_entry_draft_version": 1,
        "product_entry_draft_sha256": "9" * 64,
        "product_id": "SKU-001",
        "correlation_id": "mall-order-001",
        "idempotency_key": f"mall-style:{submission_id}",
        "payload_sha256": "0" * 64,
        "authorized_purpose": "style_candidate_generation_for_merchant_review",
        "assets": tuple(assets),
        "selected_product_photo_source_asset_id": selected,
        "style_selection": MallStyleSelectionV1(
            scene_style_id=uuid4(),
            style_card_version_id=uuid4(),
            slot_key="hero",
        ),
        "candidate_count": 3,
        "created_at": datetime.now(UTC),
        "automatic_generation_allowed": False,
        "automatic_publication_allowed": False,
        "publishable": False,
    }


def valid_handoff(*, with_product: bool = False) -> MallStyleHandoffV1:
    data = handoff_data(with_product=with_product)
    unsealed = MallStyleHandoffV1.model_construct(**data)
    data["payload_sha256"] = canonical_mall_payload_sha256(unsealed)
    return MallStyleHandoffV1.model_validate(data)


def test_label_only_contract_is_valid_but_has_no_product_subject() -> None:
    value = valid_handoff()

    assert value.selected_product_photo_source_asset_id is None
    assert {asset.asset_role for asset in value.assets} == {"label_photo"}
    assert value.automatic_generation_allowed is False
    assert value.publishable is False


def test_label_photo_can_never_claim_product_transform_authority() -> None:
    with pytest.raises(ValidationError, match="label photo is metadata-only"):
        MallStyleAssetRefV1(
            source_asset_id=uuid4(),
            source_content_sha256="a" * 64,
            asset_role="label_photo",
            mime_type="image/jpeg",
            authorized_purposes=("style_subject_transform",),
        )


def test_product_photo_requires_existing_style_asset_mapping() -> None:
    with pytest.raises(ValidationError, match="authorized style asset"):
        MallStyleAssetRefV1(
            source_asset_id=uuid4(),
            source_content_sha256="b" * 64,
            asset_role="product_photo",
            mime_type="image/jpeg",
            authorized_purposes=("style_subject_transform",),
        )


def test_contract_rejects_digest_changes_and_automatic_permissions() -> None:
    value = valid_handoff(with_product=True)
    changed = value.model_dump(mode="python")
    changed["candidate_count"] = 1
    with pytest.raises(ValidationError, match="payload digest"):
        MallStyleHandoffV1.model_validate(changed)

    escalated = value.model_dump(mode="python")
    escalated["automatic_generation_allowed"] = True
    with pytest.raises(ValidationError, match="remain false"):
        MallStyleHandoffV1.model_validate(escalated)


@pytest.mark.parametrize(
    ("filename", "model"),
    [
        ("mall-style-handoff.v1.schema.json", MallStyleHandoffV1),
        ("mall-style-selection-list.v1.schema.json", MallStyleSelectionListV1),
        (
            "mall-product-asset-import-receipt.v1.schema.json",
            MallProductAssetImportReceiptV1,
        ),
        (
            "mall-style-generation-approval.v1.schema.json",
            MallStyleGenerationApprovalV1,
        ),
        (
            "mall-style-generation-queue-receipt.v1.schema.json",
            MallStyleGenerationQueueReceiptV1,
        ),
        ("mall-style-result-list.v1.schema.json", MallStyleResultListV1),
        (
            "mall-subject-confirmation-metadata.v1.schema.json",
            MallSubjectConfirmationMetadataV1,
        ),
        ("mall-subject-inspection.v1.schema.json", MallSubjectInspectionV1),
        (
            "mall-subject-confirmation-receipt.v1.schema.json",
            MallSubjectConfirmationReceiptV1,
        ),
    ],
)
def test_checked_in_mall_contract_schema_is_current(
    filename: str, model: type[BaseModel]
) -> None:
    checked_in = json.loads((ROOT / "contracts" / filename).read_text())
    assert checked_in == model.model_json_schema()


def valid_approval_data() -> dict[str, object]:
    receipt = uuid4()
    now = datetime.now(UTC)
    data: dict[str, object] = {
        "receipt_id": receipt,
        "product_id": "SKU-001",
        "idempotency_key": f"mall-style-generation:{receipt}",
        "approval_reference": "owner-approval-001",
        "provider": "disabled-provider",
        "model": "disabled-model",
        "owner_actor_reference": "owner-001",
        "actor_role": "merchant_owner",
        "automatic_retry_allowed": False,
        "approved_candidate_count": 3,
        "approved_maximum_cost_usd": Decimal("0.36"),
        "currency": "USD",
        "approved_at": now,
        "expires_at": now + timedelta(hours=1),
        "approval_payload_sha256": "0" * 64,
        "automatic_publication_allowed": False,
        "publishable": False,
    }
    value = MallStyleGenerationApprovalV1.model_construct(**data)
    data["approval_payload_sha256"] = canonical_mall_generation_approval_sha256(value)
    return data


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_role", "supplier"),
        ("automatic_retry_allowed", True),
        ("automatic_retry_allowed", 0),
        ("approved_candidate_count", 4),
        ("provider", "changed-provider"),
        ("model", "changed-model"),
        ("owner_actor_reference", "different-owner"),
    ],
)
def test_fee_approval_rejects_escalation_and_unsigned_changes(
    field: str, value: object
) -> None:
    data = valid_approval_data()
    assert MallStyleGenerationApprovalV1.model_validate(data).publishable is False
    data[field] = value
    with pytest.raises(ValidationError):
        MallStyleGenerationApprovalV1.model_validate(data)
