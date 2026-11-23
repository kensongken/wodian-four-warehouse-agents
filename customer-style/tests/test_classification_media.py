import asyncio
import hashlib
import hmac

import httpx
from test_style_warehouse_v3_contract import ASSET_ID, eligibility

from opc_style.services.classification_media import (
    ClassificationMediaUnavailable,
    source_image_read_authorized,
    verify_source_images,
)


def test_original_bytes_are_not_read_without_active_matching_authorization() -> None:
    valid = eligibility()
    assert source_image_read_authorized(valid, now=valid.created_at)
    assert not source_image_read_authorized(
        valid.model_copy(update={"transform_authorization_status": "revoked"}),
        now=valid.created_at,
    )
    assert not source_image_read_authorized(
        valid.model_copy(update={"transform_authorization_image_set_sha256": "f" * 64}),
        now=valid.created_at,
    )
    assert not source_image_read_authorized(
        valid,
        now=valid.transform_authorization_expires_at,
    )


def test_reads_signed_image_and_verifies_exact_bytes_and_identity() -> None:
    content = b"synthetic source image bytes"
    digest = hashlib.sha256(content).hexdigest()
    original = eligibility()
    image = original.main_image.model_copy(update={"sha256": digest})
    payload = original.model_copy(update={"main_image": image, "images": (image,)})
    secret = "style-classification-media-secret"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        timestamp = request.headers["X-Signature-Timestamp"]
        body_digest = hashlib.sha256(b"").hexdigest()
        signed = (
            f"{timestamp}\nstyle\n{payload.tenant_id}\nGET\n{request.url.path}\n{body_digest}"
        ).encode()
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        assert request.headers["X-Signature"] == expected
        assert request.headers["X-Source-System"] == "style"
        assert request.url.path == (
            f"/v0.1/products/{payload.product_code}/packages/"
            f"{payload.source_material_package_version}/images/{ASSET_ID}/content"
        )
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "image/jpeg", "x-content-sha256": digest},
        )

    verified = asyncio.run(
        verify_source_images(
            payload,
            base_url="http://classification.test",
            secret=secret,
            max_bytes=1024,
            transport=httpx.MockTransport(handler),
        )
    )

    assert verified == {str(ASSET_ID): digest}
    assert len(requests) == 1


def test_rejects_bytes_whose_digest_does_not_match_the_handoff() -> None:
    payload = eligibility()

    try:
        asyncio.run(
            verify_source_images(
                payload,
                base_url="http://classification.test",
                secret="style-classification-media-secret",
                max_bytes=1024,
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        content=b"not the declared image",
                        headers={
                            "content-type": "image/jpeg",
                            "x-content-sha256": "c" * 64,
                        },
                    )
                ),
            )
        )
    except ClassificationMediaUnavailable as error:
        assert str(error) == "classification_media_integrity_failed"
    else:
        raise AssertionError("mismatched image bytes were accepted")
