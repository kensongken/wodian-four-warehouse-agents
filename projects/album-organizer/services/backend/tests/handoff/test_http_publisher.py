from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from photo_organizer.handoff.http_publisher import (
    ProductImageSetHttpPublisher,
    PublicationDisposition,
)
from photo_organizer.handoff.product_image_set import (
    ClassificationCandidateSource,
    ConfirmedClassificationSource,
    OpcExternalTenantBinding,
    PreparedImageHandoff,
    ProductCodeBinding,
    ProductImageSetSource,
    SourceImage,
    build_product_image_set_ready,
    canonical_body,
)

SECRET = b"synthetic-album-signing-secret"
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


def _event():
    source = ProductImageSetSource(
        opc_id="synthetic-opc-001",
        album_product_id="album-product-synthetic-001",
        snapshot_version=1,
        source_import_task_id="33333333-3333-4333-8333-333333333333",
        images=(
            SourceImage(
                asset_id=UUID("44444444-4444-4444-8444-444444444444"),
                sha256="a" * 64,
                mime_type="image/png",
                kind="original",
            ),
        ),
    )
    classification = ConfirmedClassificationSource(
        classification_id=UUID("55555555-5555-4555-8555-555555555555"),
        opc_id=source.opc_id,
        album_product_id=source.album_product_id,
        input_snapshot_version=source.snapshot_version,
        status="confirmed",
        confirmed_level_1="南红",
        confirmed_level_2="手串",
        confirmed_at=NOW,
        candidates=(
            ClassificationCandidateSource(
                level_1="南红", level_2="手串", confidence=0.8
            ),
        ),
    )
    return build_product_image_set_ready(
        source,
        classification,
        OpcExternalTenantBinding(
            binding_id=UUID("66666666-6666-4666-8666-666666666666"),
            binding_source="operator_provisioned",
            opc_id=source.opc_id,
            tenant_id=TENANT_ID,
        ),
        ProductCodeBinding(
            binding_id=UUID("77777777-7777-4777-8777-777777777777"),
            binding_source="operator_provisioned",
            opc_id=source.opc_id,
            album_product_id=source.album_product_id,
            product_code="SYNTHETIC-001",
        ),
        (
            PreparedImageHandoff(
                asset_id=source.images[0].asset_id,
                object_key=f"tenants/{TENANT_ID}/products/SYNTHETIC-001/main.png",
                role="main_candidate",
                quality_confidence=0.9,
                storage_status="ready",
            ),
        ),
    )


def _expected_signature(request: httpx.Request) -> str:
    timestamp = request.headers["X-Signature-Timestamp"]
    body_digest = hashlib.sha256(request.content).hexdigest()
    message = (
        f"{timestamp}\nalbum\n{TENANT_ID}\nPOST\n"
        "/v1/integrations/album/product-image-sets\n"
        f"{body_digest}"
    ).encode()
    return hmac.new(SECRET, message, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    ((202, PublicationDisposition.DELIVERED), (200, PublicationDisposition.DELIVERED)),
)
async def test_publisher_sends_frozen_canonical_body_and_accepts_receiver_success(
    status_code: int,
    expected: PublicationDisposition,
) -> None:
    event = _event()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/integrations/album/product-image-sets"
        assert request.content == canonical_body(event)
        assert request.headers["X-Source-System"] == "album"
        assert request.headers["X-Tenant-Id"] == str(TENANT_ID)
        assert request.headers["X-Signature"] == _expected_signature(request)
        return httpx.Response(status_code)

    publisher = ProductImageSetHttpPublisher(
        "http://127.0.0.1:18020/v1/integrations/album/product-image-sets",
        SECRET,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )

    receipt = await publisher.publish(event)

    assert receipt.disposition is expected
    assert receipt.status_code == status_code


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", (401, 409, 422))
async def test_publisher_stops_retrying_receiver_rejections(status_code: int) -> None:
    publisher = ProductImageSetHttpPublisher(
        "http://127.0.0.1:18020/v1/integrations/album/product-image-sets",
        SECRET,
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code)),
        clock=lambda: NOW,
    )

    receipt = await publisher.publish(_event())

    assert receipt.disposition is PublicationDisposition.PERMANENT_FAILURE
    assert receipt.error_code == f"http_{status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", (408, 425, 429, 500, 503))
async def test_publisher_marks_temporary_receiver_failures_retryable(
    status_code: int,
) -> None:
    publisher = ProductImageSetHttpPublisher(
        "http://127.0.0.1:18020/v1/integrations/album/product-image-sets",
        SECRET,
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code)),
        clock=lambda: NOW,
    )

    receipt = await publisher.publish(_event())

    assert receipt.disposition is PublicationDisposition.RETRYABLE_FAILURE
    assert receipt.error_code == f"http_{status_code}"
    assert receipt.retry_after_seconds == 30


@pytest.mark.asyncio
async def test_publisher_uses_fixed_retry_backoff_for_later_attempts() -> None:
    publisher = ProductImageSetHttpPublisher(
        "http://127.0.0.1:18020/v1/integrations/album/product-image-sets",
        SECRET,
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        clock=lambda: NOW,
    )

    receipt = await publisher.publish(_event(), attempt=3)

    assert receipt.disposition is PublicationDisposition.RETRYABLE_FAILURE
    assert receipt.retry_after_seconds == 600


@pytest.mark.asyncio
async def test_publisher_refuses_redirects_and_transport_errors() -> None:
    redirect = ProductImageSetHttpPublisher(
        "http://127.0.0.1:18020/v1/integrations/album/product-image-sets",
        SECRET,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                308, headers={"Location": "http://bad.invalid"}
            )
        ),
        clock=lambda: NOW,
    )
    unreachable = ProductImageSetHttpPublisher(
        "http://127.0.0.1:18020/v1/integrations/album/product-image-sets",
        SECRET,
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline"))
        ),
        clock=lambda: NOW,
    )

    redirect_receipt = await redirect.publish(_event())
    error_receipt = await unreachable.publish(_event())

    assert redirect_receipt.disposition is PublicationDisposition.PERMANENT_FAILURE
    assert redirect_receipt.error_code == "redirect_refused"
    assert error_receipt.disposition is PublicationDisposition.RETRYABLE_FAILURE
    assert error_receipt.error_code == "network_error"
