from datetime import UTC, datetime
from uuid import UUID

import pytest

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    canonical_classification_sha256,
)
from product_engine.repositories.style_handoffs import (
    PreparedStyleEligibility,
    StyleClassificationAsset,
    StyleClassificationSource,
    StyleHandoffView,
    StyleTransformAuthorizationView,
    classification_image_set_sha256,
)
from product_engine.services.style_eligibility_v2 import (
    StyleEligibilityFailure,
    StyleEligibilityPreparer,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
PRODUCT_ID = UUID("33333333-3333-4333-8333-333333333333")
SOURCE_IMAGE_ID = UUID("44444444-4444-4444-8444-444444444444")
ASSET_ID = UUID("55555555-5555-4555-8555-555555555555")
CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")
HANDOFF_ID = UUID("77777777-7777-4777-8777-777777777777")
ELIGIBILITY_ID = UUID("88888888-8888-4888-8888-888888888888")
AUTHORIZATION_ID = UUID("99999999-9999-4999-8999-999999999999")
NOW = datetime(2026, 9, 20, 8, tzinfo=UTC)


def source(**updates: object) -> StyleClassificationSource:
    asset = StyleClassificationAsset(
        asset_id=ASSET_ID,
        source_image_id=SOURCE_IMAGE_ID,
        position=0,
        role="main_candidate",
        sha256="a" * 64,
        mime_type="image/jpeg",
    )
    command = ClassificationConfirmationCommand(
        request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        product_code="BCNHDIY914",
        local_batch_id="ledger-revalidation",
        base_material_package_version=1,
        material_label="信息不足",
        product_form="earring",
        main_asset_id=ASSET_ID,
        visual_observations=["金色圆环外形"],
        selling_point_candidates=["日常佩戴"],
        inventory_status=None,
        price_status="pending",
        price_candidate=None,
        service_rule_summaries=[],
        review_source="local_trainer_review",
        assets=[
            ClassificationAssetConfirmation(
                asset_id=ASSET_ID,
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            )
        ],
    )
    values: dict[str, object] = {
        "classification_id": CLASSIFICATION_ID,
        "tenant_id": TENANT_ID,
        "product_id": PRODUCT_ID,
        "product_code": "BCNHDIY914",
        "version": 1,
        "is_latest": True,
        "base_material_package_version": 1,
        "material_label": "信息不足",
        "material_evidence": "information_insufficient",
        "product_form": "earring",
        "main_source_image_id": SOURCE_IMAGE_ID,
        "visual_observations": ("金色圆环外形",),
        "selling_point_candidates": ("日常佩戴",),
        "inventory_status": None,
        "price_status": "pending",
        "price_candidate": None,
        "service_rule_summaries": (),
        "review_source": "local_trainer_review",
        "request_id": command.request_id,
        "content_sha256": canonical_classification_sha256(command),
        "source_image_set_sha256": classification_image_set_sha256((asset,)),
        "assets": (asset,),
    }
    values.update(updates)
    return StyleClassificationSource(**values)  # type: ignore[arg-type]


def authorization(
    *,
    expires_at: datetime = datetime(2026, 9, 27, 8, tzinfo=UTC),
    revoked_at: datetime | None = None,
) -> StyleTransformAuthorizationView:
    return StyleTransformAuthorizationView(
        authorization_id=AUTHORIZATION_ID,
        authorization_request_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        tenant_id=TENANT_ID,
        classification_id=CLASSIFICATION_ID,
        source_image_set_sha256=source().source_image_set_sha256,
        scope="local_style_acceptance_only",
        expires_at=expires_at,
        authorized_at=NOW,
        revoked_at=revoked_at,
    )


class FakeRepository:
    def __init__(
        self,
        classification: StyleClassificationSource | None,
        transform_authorization: StyleTransformAuthorizationView | None = None,
    ) -> None:
        self.classification = classification
        self.transform_authorization = transform_authorization
        self.appended = []

    async def load_latest_source(self, tenant_id: UUID, classification_id: UUID):
        assert tenant_id == TENANT_ID
        assert classification_id == CLASSIFICATION_ID
        return self.classification

    async def load_latest_authorization(
        self,
        tenant_id: UUID,
        classification_id: UUID,
        source_image_set_sha256: str,
    ):
        assert tenant_id == TENANT_ID
        assert classification_id == CLASSIFICATION_ID
        return self.transform_authorization

    async def append_eligibility(self, candidate):
        self.appended.append(candidate)
        return PreparedStyleEligibility(
            eligibility_input_id=ELIGIBILITY_ID,
            payload=candidate,
            handoff=StyleHandoffView(
                handoff_id=HANDOFF_ID,
                product_code=candidate.product_code,
                payload_version=1,
                payload_sha256=candidate.content_sha256,
                idempotency_key=f"style:{TENANT_ID}:{ELIGIBILITY_ID}",
                status="prepared",
                decision_receipt_id=None,
                rejection_code=None,
                prepared_at=NOW,
                last_attempted_at=None,
                settled_at=None,
            ),
            created=True,
        )


async def test_prepare_keeps_information_insufficient_without_authorization() -> None:
    repository = FakeRepository(source())

    result = await StyleEligibilityPreparer(repository, now=lambda: NOW).prepare(
        TENANT_ID, CLASSIFICATION_ID
    )

    assert result.payload.material.evidence == "information_insufficient"
    assert result.payload.transform_authorization_id is None
    assert result.payload.transform_authorization_status == "none"
    assert result.handoff.status == "prepared"


async def test_prepare_rejects_non_latest_classification() -> None:
    repository = FakeRepository(source(is_latest=False))

    with pytest.raises(StyleEligibilityFailure, match="classification_not_latest"):
        await StyleEligibilityPreparer(repository, now=lambda: NOW).prepare(
            TENANT_ID, CLASSIFICATION_ID
        )


@pytest.mark.parametrize(
    ("transform_authorization", "expected_status"),
    [
        (authorization(), "active"),
        (authorization(expires_at=datetime(2026, 9, 19, tzinfo=UTC)), "expired"),
        (authorization(revoked_at=datetime(2026, 9, 20, 7, tzinfo=UTC)), "revoked"),
    ],
)
async def test_prepare_snapshots_authorization_state(
    transform_authorization: StyleTransformAuthorizationView,
    expected_status: str,
) -> None:
    result = await StyleEligibilityPreparer(
        FakeRepository(source(), transform_authorization), now=lambda: NOW
    ).prepare(TENANT_ID, CLASSIFICATION_ID)

    assert result.payload.transform_authorization_id == AUTHORIZATION_ID
    assert result.payload.transform_authorization_status == expected_status


async def test_prepare_rejects_tampered_classification_digest() -> None:
    repository = FakeRepository(source(content_sha256="f" * 64))

    with pytest.raises(StyleEligibilityFailure, match="classification_ledger_conflict"):
        await StyleEligibilityPreparer(repository, now=lambda: NOW).prepare(
            TENANT_ID, CLASSIFICATION_ID
        )
