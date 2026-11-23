from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from product_engine.api.local_app import create_local_app
from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityInputV2,
    canonical_style_eligibility_v2_sha256,
)
from product_engine.repositories.style_handoffs import (
    PreparedStyleEligibility,
    StoredStyleHandoff,
    StyleHandoffView,
    StyleTransformAuthorizationView,
)
from product_engine.services.runtime_style_handoffs import RuntimeStyleHandoffFailure

TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CLASSIFICATION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
AUTHORIZATION_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
AUTHORIZATION_REQUEST_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
ELIGIBILITY_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
HANDOFF_ID = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
RECEIPT_ID = UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2026, 9, 20, 8, tzinfo=UTC)


def eligibility() -> StyleEligibilityInputV2:
    value = StyleEligibilityInputV2(
        tenant_id=TENANT_ID,
        product_code="BCNHDIY914",
        eligibility_input_version=1,
        source_material_package_version=1,
        source_classification_id=CLASSIFICATION_ID,
        source_classification_version=1,
        source_classification_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        main_image={
            "asset_id": "22222222-2222-4222-8222-222222222222",
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        },
        images=(
            {
                "asset_id": "22222222-2222-4222-8222-222222222222",
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        material={"value": "信息不足", "evidence": "information_insufficient"},
        form={"value": "earring", "evidence": "human_confirmed"},
        transform_authorization_id=None,
        transform_authorization_status="none",
        created_at=NOW,
        content_sha256="0" * 64,
    )
    return value.model_copy(update={"content_sha256": canonical_style_eligibility_v2_sha256(value)})


def handoff(*, rejected: bool = False) -> StyleHandoffView:
    value = eligibility()
    return StyleHandoffView(
        handoff_id=HANDOFF_ID,
        product_code=value.product_code,
        payload_version=1,
        payload_sha256=value.content_sha256,
        idempotency_key=f"style:{TENANT_ID}:{ELIGIBILITY_ID}",
        status="rejected" if rejected else "prepared",
        decision_receipt_id=RECEIPT_ID if rejected else None,
        rejection_code="classification_material_unconfirmed" if rejected else None,
        prepared_at=NOW,
        last_attempted_at=NOW if rejected else None,
        settled_at=NOW if rejected else None,
    )


class FakeStyleHandoffService:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, UUID]] = []

    def _fail(self) -> None:
        if self.failure:
            raise RuntimeStyleHandoffFailure(self.failure)

    async def prepare(self, classification_id: UUID) -> PreparedStyleEligibility:
        self.calls.append(("prepare", classification_id))
        self._fail()
        return PreparedStyleEligibility(
            eligibility_input_id=ELIGIBILITY_ID,
            payload=eligibility(),
            handoff=handoff(),
            created=True,
        )

    async def evaluate(self, handoff_id: UUID) -> StoredStyleHandoff:
        self.calls.append(("evaluate", handoff_id))
        self._fail()
        return StoredStyleHandoff(
            eligibility_input_id=ELIGIBILITY_ID,
            payload=eligibility(),
            handoff=handoff(rejected=True),
        )

    async def read(self, handoff_id: UUID) -> StoredStyleHandoff:
        self.calls.append(("read", handoff_id))
        self._fail()
        return StoredStyleHandoff(
            eligibility_input_id=ELIGIBILITY_ID,
            payload=eligibility(),
            handoff=handoff(rejected=True),
        )

    async def authorize(self, classification_id, command):
        self.calls.append(("authorize", classification_id))
        self._fail()
        return StyleTransformAuthorizationView(
            authorization_id=AUTHORIZATION_ID,
            authorization_request_id=command.authorization_request_id,
            tenant_id=TENANT_ID,
            classification_id=classification_id,
            source_image_set_sha256="b" * 64,
            scope="local_style_acceptance_only",
            expires_at=command.expires_at,
            authorized_at=NOW,
            revoked_at=None,
        )

    async def revoke(self, authorization_id):
        self.calls.append(("revoke", authorization_id))
        self._fail()
        return StyleTransformAuthorizationView(
            authorization_id=authorization_id,
            authorization_request_id=AUTHORIZATION_REQUEST_ID,
            tenant_id=TENANT_ID,
            classification_id=CLASSIFICATION_ID,
            source_image_set_sha256="b" * 64,
            scope="local_style_acceptance_only",
            expires_at=datetime(2026, 10, 1, tzinfo=UTC),
            authorized_at=NOW,
            revoked_at=NOW,
        )


def client(tmp_path, service=None) -> TestClient:
    return TestClient(create_local_app(tmp_path / "data", style_handoff_service=service))


def test_prepare_accepts_only_classification_identifier(tmp_path) -> None:
    response = client(tmp_path, FakeStyleHandoffService()).post(
        f"/local/api/classification-versions/{CLASSIFICATION_ID}/prepare-style-eligibility"
    )

    assert response.status_code == 201
    assert response.json()["eligibility"]["material"]["evidence"] == ("information_insufficient")
    assert response.json()["eligibility_input_id"] == str(ELIGIBILITY_ID)


def test_current_material_failure_is_successful_rejection(tmp_path) -> None:
    response = client(tmp_path, FakeStyleHandoffService()).post(
        f"/local/api/style-warehouse-handoffs/{HANDOFF_ID}/evaluate"
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "rejected"
    assert response.json()["rejection_code"] == "classification_material_unconfirmed"
    assert response.json()["decision_receipt_id"] == str(RECEIPT_ID)


def test_authorization_requires_explicit_request_and_expiry(tmp_path) -> None:
    app = client(tmp_path, FakeStyleHandoffService())
    path = f"/local/api/classification-versions/{CLASSIFICATION_ID}/style-transform-authorizations"
    assert app.post(path, json={}).status_code == 422
    response = app.post(
        path,
        json={
            "authorization_request_id": str(AUTHORIZATION_REQUEST_ID),
            "scope": "local_style_acceptance_only",
            "expires_at": "2026-10-01T00:00:00Z",
        },
    )
    assert response.status_code == 201
    assert response.json()["authorization_id"] == str(AUTHORIZATION_ID)


def test_revoke_is_explicit_and_idempotent(tmp_path) -> None:
    app = client(tmp_path, FakeStyleHandoffService())
    path = f"/local/api/style-transform-authorizations/{AUTHORIZATION_ID}/revoke"
    first = app.post(path)
    second = app.post(path)

    assert first.status_code == second.status_code == 200
    assert second.json()["revoked_at"] == first.json()["revoked_at"]


def test_read_restores_original_rejection(tmp_path) -> None:
    response = client(tmp_path, FakeStyleHandoffService()).get(
        f"/local/api/style-warehouse-handoffs/{HANDOFF_ID}"
    )
    assert response.status_code == 200
    assert response.json()["decision_receipt_id"] == str(RECEIPT_ID)
    assert response.json()["decision"] == "rejected"


@pytest.mark.parametrize(
    ("error_code", "status_code"),
    [
        ("style_handoff_unconfigured", 404),
        ("classification_not_found", 404),
        ("style_handoff_not_found", 404),
        ("style_authorization_not_found", 404),
        ("classification_not_latest", 409),
        ("classification_ledger_conflict", 409),
        ("style_handoff_conflict", 409),
        ("style_payload_hash_mismatch", 409),
    ],
)
def test_failures_are_allowlisted_and_safe(tmp_path, error_code: str, status_code: int) -> None:
    service = (
        None if error_code == "style_handoff_unconfigured" else FakeStyleHandoffService(error_code)
    )
    response = client(tmp_path, service).post(
        f"/local/api/classification-versions/{CLASSIFICATION_ID}/prepare-style-eligibility"
    )
    assert response.status_code == status_code
    assert response.json()["error_code"] == error_code
    assert "traceback" not in str(response.json()).casefold()
