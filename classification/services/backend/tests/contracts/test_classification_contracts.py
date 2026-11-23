from uuid import UUID

import pytest
from pydantic import ValidationError

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    canonical_classification_sha256,
)


def command() -> ClassificationConfirmationCommand:
    return ClassificationConfirmationCommand(
        request_id=UUID("11111111-1111-4111-8111-111111111111"),
        product_code="BCNHDIY914",
        local_batch_id="BATCH-20260920033509-8C21C203",
        base_material_package_version=1,
        material_label="信息不足",
        product_form="earring",
        main_asset_id=UUID("22222222-2222-4222-8222-222222222222"),
        visual_observations=[],
        selling_point_candidates=[],
        inventory_status=None,
        price_status="pending",
        price_candidate=None,
        service_rule_summaries=[],
        review_source="local_trainer_review",
        assets=[
            ClassificationAssetConfirmation(
                asset_id=UUID("22222222-2222-4222-8222-222222222222"),
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            ),
            ClassificationAssetConfirmation(
                asset_id=UUID("33333333-3333-4333-8333-333333333333"),
                position=1,
                role="gallery",
                sha256="b" * 64,
            ),
        ],
    )


def test_classification_fingerprint_excludes_request_id() -> None:
    first = command()
    replay = first.model_copy(update={"request_id": UUID("44444444-4444-4444-8444-444444444444")})
    assert canonical_classification_sha256(first) == canonical_classification_sha256(replay)


def test_classification_fingerprint_changes_with_asset_order() -> None:
    first = command()
    changed = first.model_copy(update={"assets": list(reversed(first.assets))})
    assert canonical_classification_sha256(first) != canonical_classification_sha256(changed)


def test_price_candidate_requires_human_confirmed_status() -> None:
    payload = command().model_dump()
    payload["price_candidate"] = {"currency": "CNY", "amount": "100.00"}
    with pytest.raises(ValidationError):
        ClassificationConfirmationCommand.model_validate(payload)
