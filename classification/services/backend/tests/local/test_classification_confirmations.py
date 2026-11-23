from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from product_engine.api.local_app import create_local_app
from product_engine.contracts.classification import ClassificationConfirmationReceipt
from product_engine.services.classification_confirmations import (
    ClassificationConfirmationFailure,
)
from product_engine.services.local_upload_batches import LocalUploadBatchStore
from product_engine.services.template_registry import TemplateRecord

PRODUCT_CODE = "BCNHDIY914"
REQUEST_ID = UUID("11111111-1111-4111-8111-111111111111")
CLASSIFICATION_ID = UUID("66666666-6666-4666-8666-666666666666")


class Writer:
    def __init__(self, *, disposition: str = "created", failure: str | None = None):
        self.disposition = disposition
        self.failure = failure
        self.calls = []

    async def confirm(
        self,
        *,
        product_code: str,
        package_version: int,
        request_id: UUID,
        local_batch_id: str,
    ) -> ClassificationConfirmationReceipt:
        self.calls.append((product_code, package_version, request_id, local_batch_id))
        if self.failure is not None:
            raise ClassificationConfirmationFailure(self.failure)
        return ClassificationConfirmationReceipt(
            request_id=request_id,
            product_code=product_code,
            classification_version=1,
            classification_id=CLASSIFICATION_ID,
            content_sha256="a" * 64,
            material_package_version=package_version,
            material_label="信息不足",
            material_evidence="information_insufficient",
            disposition=self.disposition,
            downstream_status="not_sent",
        )


def seed_batch(data_root, *, complete: bool = True) -> str:
    store = LocalUploadBatchStore(data_root / "upload-batches.json")
    batch = store.create(expected_count=2)
    for position, role in enumerate(("main", "detail")):
        item = store.add_image(
            batch.batch_id,
            position,
            f"{PRODUCT_CODE}-{position}.jpg",
            TemplateRecord(
                id=uuid4(),
                tenant_id=uuid4(),
                template_code=f"TEMPLATE-{position}",
                version=1,
                category="product",
                image_type="source",
                object_key=f"local/{position}.jpg",
                sha256=("a" if position == 0 else "b") * 64,
                mime_type="image/jpeg",
                byte_size=100,
            ),
            product_code=PRODUCT_CODE,
            product_sequence=1,
            image_role=role,
            source_system="album_organizer",
        )
        if complete or position == 0:
            store.mark_reviewed(batch.batch_id, item.item_id)
    return batch.batch_id


def endpoint(batch_id: str) -> str:
    return f"/local/api/album-review-inbox/{PRODUCT_CODE}/versions/1/classification-confirmations"


def generic_endpoint() -> str:
    prefix = f"/local/api/formal-product-packages/{PRODUCT_CODE}"
    return f"{prefix}/versions/1/classification-confirmations"


def payload(batch_id: str) -> dict[str, str]:
    return {"request_id": str(REQUEST_ID), "local_batch_id": batch_id}


def test_created_confirmation_returns_201_and_safe_receipt(tmp_path) -> None:
    data_root = tmp_path / "data"
    batch_id = seed_batch(data_root)
    writer = Writer()
    client = TestClient(create_local_app(data_root, classification_confirmation_writer=writer))

    response = client.post(endpoint(batch_id), json=payload(batch_id))

    assert response.status_code == 201
    assert response.json()["classification_version"] == 1
    assert response.json()["disposition"] == "created"
    assert response.json()["downstream_status"] == "not_sent"
    assert writer.calls == [(PRODUCT_CODE, 1, REQUEST_ID, batch_id)]


def test_generic_and_album_classification_routes_are_compatible(tmp_path) -> None:
    responses = []
    for name, path_factory in (("generic", generic_endpoint), ("album", lambda: endpoint(""))):
        data_root = tmp_path / name
        batch_id = seed_batch(data_root)
        writer = Writer()
        client = TestClient(create_local_app(data_root, classification_confirmation_writer=writer))
        responses.append(client.post(path_factory(), json=payload(batch_id)))

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()


def test_duplicate_confirmation_returns_200_with_same_id(tmp_path) -> None:
    data_root = tmp_path / "data"
    batch_id = seed_batch(data_root)
    writer = Writer(disposition="duplicate")
    client = TestClient(create_local_app(data_root, classification_confirmation_writer=writer))

    response = client.post(endpoint(batch_id), json=payload(batch_id))

    assert response.status_code == 200
    assert response.json()["classification_id"] == str(CLASSIFICATION_ID)
    assert response.json()["disposition"] == "duplicate"


def test_unconfigured_writer_returns_404(tmp_path) -> None:
    data_root = tmp_path / "data"
    batch_id = seed_batch(data_root)
    response = TestClient(create_local_app(data_root)).post(
        endpoint(batch_id), json=payload(batch_id)
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "classification_writeback_unconfigured"


def test_incomplete_local_review_returns_409_without_calling_writer(tmp_path) -> None:
    data_root = tmp_path / "data"
    batch_id = seed_batch(data_root, complete=False)
    writer = Writer()
    response = TestClient(
        create_local_app(data_root, classification_confirmation_writer=writer)
    ).post(endpoint(batch_id), json=payload(batch_id))

    assert response.status_code == 409
    assert response.json()["error_code"] == "product_review_incomplete"
    assert writer.calls == []


@pytest.mark.parametrize(
    "error_code",
    [
        "product_review_identity_mismatch",
        "product_review_incomplete",
        "product_review_value_mismatch",
        "classification_context_not_found",
        "stale_material_package_version",
        "classification_asset_mismatch",
        "classification_request_conflict",
        "classification_ledger_conflict",
    ],
)
def test_confirmation_failures_return_stable_safe_409(tmp_path, error_code) -> None:
    data_root = tmp_path / error_code
    batch_id = seed_batch(data_root)
    writer = Writer(failure=error_code)
    response = TestClient(
        create_local_app(data_root, classification_confirmation_writer=writer)
    ).post(endpoint(batch_id), json=payload(batch_id))

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == error_code
    serialized = str(body)
    assert "traceback" not in serialized.casefold()
    assert "postgresql" not in serialized.casefold()
    assert "object_key" not in serialized.casefold()
    assert str(tmp_path) not in serialized
