from uuid import UUID

from fastapi.testclient import TestClient

from product_engine.contracts.material_evidence import (
    MaterialEvidenceReceipt,
    MaterialEvidenceSnapshot,
)

REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")
BASE_ID = UUID("22222222-2222-4222-8222-222222222222")
EVIDENCE_ID = UUID("33333333-3333-4333-8333-333333333333")
CLASSIFICATION_ID = UUID("44444444-4444-4444-8444-444444444444")
OTHER_TENANT = UUID("55555555-5555-4555-8555-555555555555")
PRODUCT_CODE = "BCNHDIY914"


class FakeMaterialEvidenceRuntime:
    async def register(self, product_code: str, request: object) -> MaterialEvidenceReceipt:
        assert product_code == PRODUCT_CODE
        return MaterialEvidenceReceipt(
            request_id=REQUEST_ID,
            evidence=MaterialEvidenceSnapshot(
                evidence_id=EVIDENCE_ID,
                material_label="南红",
                claim_basis="merchant_declared",
                evidence_kind="merchant_declaration",
                evidence_content_sha256="a" * 64,
            ),
            classification_id=CLASSIFICATION_ID,
            classification_version=2,
            disposition="created",
        )


def valid_body() -> dict[str, object]:
    return {
        "request_id": str(REQUEST_ID),
        "base_classification_id": str(BASE_ID),
        "material_label": "南红",
        "claim_basis": "merchant_declared",
        "evidence_kind": "merchant_declaration",
        "source_summary": "商家确认当前商品材质为南红",
        "acknowledge_not_authentication": True,
    }


def build_client(tmp_path) -> TestClient:
    from product_engine.api.local_app import create_local_app

    return TestClient(
        create_local_app(
            tmp_path / "local-data",
            material_evidence_runtime=FakeMaterialEvidenceRuntime(),
        )
    )


def test_register_api_never_accepts_tenant_or_object_key(tmp_path) -> None:
    client = build_client(tmp_path)
    response = client.post(
        f"/local/api/products/{PRODUCT_CODE}/material-evidence",
        json={
            **valid_body(),
            "tenant_id": str(OTHER_TENANT),
            "object_key": "escape",
        },
    )

    assert response.status_code == 422


def test_merchant_declaration_registration_returns_receipt(tmp_path) -> None:
    response = build_client(tmp_path).post(
        f"/local/api/products/{PRODUCT_CODE}/material-evidence",
        json=valid_body(),
    )

    assert response.status_code == 201
    assert response.json()["evidence"]["claim_basis"] == "merchant_declared"
    assert response.json()["downstream_status"] == "not_sent"
