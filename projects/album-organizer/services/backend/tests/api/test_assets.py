import asyncio
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.models.user import User


def _headers(client: TestClient, seeded_invite, fake_otp) -> dict[str, str]:
    phone = "+8613800013001"
    client.post(
        "/v1/auth/request-code",
        json={"phone_e164": phone, "invite_code": seeded_invite.code},
    )
    response = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": phone,
            "code": fake_otp.last_code_for(phone),
            "device_id": "asset-test",
        },
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


def _initiate(
    client: TestClient,
    headers: dict[str, str],
    task_id: str,
    *,
    local_id: str = "ios:1",
):
    return client.post(
        f"/v1/tasks/{task_id}/assets/initiate",
        headers=headers,
        json={
            "local_asset_id": local_id,
            "filename": "IMG.jpg",
            "media_type": "image/jpeg",
            "byte_size": 8,
            "sha256": "c" * 64,
        },
    )


def test_initiate_sign_complete_and_repeat_are_resumable(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """A repeat completion must not re-complete storage or change uploaded count."""
    headers = _headers(client, seeded_invite, fake_otp)
    task = client.post("/v1/tasks", headers=headers, json={"name": "Vacation"}).json()
    initiated = _initiate(client, headers, task["id"])
    asset_id = initiated.json()["asset_id"]
    signed = client.post(
        f"/v1/assets/{asset_id}/parts/sign", headers=headers, json={"part_number": 1}
    )
    first = client.post(
        f"/v1/assets/{asset_id}/complete",
        headers=headers,
        json={"parts": [{"part_number": 1, "etag": "etag"}]},
    )
    second = client.post(
        f"/v1/assets/{asset_id}/complete",
        headers=headers,
        json={"parts": [{"part_number": 1, "etag": "etag"}]},
    )
    updated = client.get(f"/v1/tasks/{task['id']}", headers=headers)

    assert initiated.status_code == 201
    assert (signed.status_code, signed.json()["expires_seconds"]) == (200, 900)
    assert first.json()["status"] == second.json()["status"] == "uploaded"
    assert updated.json()["uploaded_assets"] == 1
    assert _app(client).state.upload_service.storage.complete_call_count == 1


def test_initiate_exposes_durable_duplicate_receipt_without_second_upload(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Omitting duplicate disposition/provenance makes a non-counting result ambiguous."""
    headers = _headers(client, seeded_invite, fake_otp)
    task = client.post("/v1/tasks", headers=headers, json={"name": "Duplicate"}).json()

    canonical = _initiate(client, headers, task["id"], local_id="ios:canonical")
    duplicate = _initiate(client, headers, task["id"], local_id="ios:duplicate")
    updated = client.get(f"/v1/tasks/{task['id']}", headers=headers)

    assert canonical.status_code == duplicate.status_code == 201
    assert canonical.json()["disposition"] == "accepted"
    assert canonical.json()["canonical_asset_id"] is None
    assert duplicate.json()["disposition"] == "duplicate"
    assert duplicate.json()["asset_id"] != canonical.json()["asset_id"]
    assert duplicate.json()["canonical_asset_id"] == canonical.json()["asset_id"]
    assert duplicate.json()["already_uploaded"] is True
    assert duplicate.json()["provider_upload_id"] is None
    assert duplicate.json()["completed_parts"] == []
    assert updated.json()["total_assets"] == 1
    assert _app(client).state.upload_service.storage.create_call_count == 1


def test_openapi_additively_describes_asset_duplicate_disposition(
    client: TestClient,
) -> None:
    """A backend-only field without a schema contract cannot be consumed safely."""
    schema = _app(client).openapi()["components"]["schemas"]["AssetInitResponse"]
    properties = schema["properties"]

    assert set(properties["disposition"]["enum"]) == {"accepted", "duplicate"}
    assert properties["canonical_asset_id"]["anyOf"][1]["type"] == "null"
    assert "asset_id" in schema["required"]
    assert "already_uploaded" in schema["required"]


def test_assets_reject_invalid_mime_size_and_parts(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Permitting unsupported input or unordered completion parts breaks boundary safety."""
    headers = _headers(client, seeded_invite, fake_otp)
    task = client.post("/v1/tasks", headers=headers, json={"name": "Limits"}).json()
    bad_mime = client.post(
        f"/v1/tasks/{task['id']}/assets/initiate",
        headers=headers,
        json={
            "local_asset_id": "raw",
            "filename": "RAW",
            "media_type": "image/raw",
            "byte_size": 1,
            "sha256": "d" * 64,
        },
    )
    too_large = client.post(
        f"/v1/tasks/{task['id']}/assets/initiate",
        headers=headers,
        json={
            "local_asset_id": "big",
            "filename": "BIG",
            "media_type": "image/png",
            "byte_size": 104857601,
            "sha256": "d" * 64,
        },
    )
    asset_id = _initiate(client, headers, task["id"]).json()["asset_id"]
    invalid_parts = client.post(
        f"/v1/assets/{asset_id}/complete",
        headers=headers,
        json={
            "parts": [
                {"part_number": 2, "etag": "two"},
                {"part_number": 1, "etag": "one"},
            ]
        },
    )

    assert (
        bad_mime.status_code,
        bad_mime.json()["detail"],
        too_large.status_code,
        too_large.json()["detail"],
    ) == (415, "unsupported_media_type", 413, "image_too_large")
    assert (invalid_parts.status_code, invalid_parts.json()["detail"]) == (
        422,
        "invalid_parts",
    )


def test_all_malformed_completion_parts_use_stable_detail(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Leaking framework validation shapes makes resumable-client recovery unstable."""
    headers = _headers(client, seeded_invite, fake_otp)
    task = client.post(
        "/v1/tasks", headers=headers, json={"name": "Part errors"}
    ).json()
    asset_id = _initiate(client, headers, task["id"]).json()["asset_id"]
    payloads = [
        {},
        {"parts": None},
        {"parts": "not-a-list"},
        {"parts": [None]},
        {"parts": [{}]},
        {"parts": [{"part_number": None, "etag": "etag"}]},
        {"parts": [{"part_number": "one", "etag": "etag"}]},
        {"parts": [{"part_number": 1, "etag": None}]},
        {"parts": [{"part_number": 1, "etag": 12}]},
        {"parts": []},
        {"parts": [{"part_number": 0, "etag": "etag"}]},
        {"parts": [{"part_number": 10001, "etag": "etag"}]},
        {"parts": [{"part_number": 1, "etag": ""}]},
        {"parts": [{"part_number": 1, "etag": "x" * 257}]},
        {
            "parts": [
                {"part_number": 1, "etag": "one"},
                {"part_number": 1, "etag": "two"},
            ]
        },
        {"parts": [{"part_number": 1, "etag": "etag"}] * 10_001},
    ]
    responses = [
        client.post(f"/v1/assets/{asset_id}/complete", headers=headers, json=payload)
        for payload in payloads
    ]

    assert [
        (response.status_code, response.json()["detail"]) for response in responses
    ] == [(422, "invalid_parts")] * len(payloads)


def test_complete_invalid_asset_id_keeps_standard_fastapi_validation_detail(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Only malformed parts bodies receive the resumable-client error normalization."""
    headers = _headers(client, seeded_invite, fake_otp)
    response = client.post(
        "/v1/assets/not-a-uuid/complete",
        headers=headers,
        json={"parts": [{"part_number": 1, "etag": "etag"}]},
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_existing_uploaded_initiation_is_idempotent_after_task_cancel(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Sealing a task must not turn an already-uploaded local retry into an error."""
    headers = _headers(client, seeded_invite, fake_otp)
    task = client.post("/v1/tasks", headers=headers, json={"name": "Idempotent"}).json()
    initiated = _initiate(client, headers, task["id"], local_id="already")
    asset_id = initiated.json()["asset_id"]
    client.post(
        f"/v1/assets/{asset_id}/complete",
        headers=headers,
        json={"parts": [{"part_number": 1, "etag": "etag"}]},
    )
    client.post(f"/v1/tasks/{task['id']}/cancel", headers=headers)
    repeated = _initiate(client, headers, task["id"], local_id="already")

    assert repeated.status_code == 201
    assert repeated.json()["already_uploaded"] is True


def test_unsupported_keeps_metadata_but_not_accepted_count(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Counting skipped metadata as accepted would incorrectly consume the task cap."""
    headers = _headers(client, seeded_invite, fake_otp)
    task = client.post("/v1/tasks", headers=headers, json={"name": "Mixed"}).json()
    first = client.post(
        f"/v1/tasks/{task['id']}/assets/unsupported",
        headers=headers,
        json={
            "local_asset_id": "android:raw",
            "filename": "RAW_1.dng",
            "media_type": "image/x-adobe-dng",
            "byte_size": 55,
            "reason": "unsupported_media_type",
        },
    )
    second = client.post(
        f"/v1/tasks/{task['id']}/assets/unsupported",
        headers=headers,
        json={
            "local_asset_id": "android:raw",
            "filename": "RAW_1.dng",
            "media_type": "image/x-adobe-dng",
            "byte_size": 55,
            "reason": "unsupported_media_type",
        },
    )
    updated = client.get(f"/v1/tasks/{task['id']}", headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert (updated.json()["total_assets"], updated.json()["unsupported_assets"]) == (
        0,
        1,
    )


def test_failed_asset_requires_explicit_retry_to_start_another_upload(
    client: TestClient, seeded_invite, fake_otp
) -> None:
    """Restarting an arbitrary local ID would overwrite failure history without retry."""
    headers = _headers(client, seeded_invite, fake_otp)
    task = client.post("/v1/tasks", headers=headers, json={"name": "Retry"}).json()
    initiated = _initiate(client, headers, task["id"], local_id="android:retry")
    asset_id = initiated.json()["asset_id"]
    failed = client.post(
        f"/v1/assets/{asset_id}/fail",
        headers=headers,
        json={"code": "read_error", "message": "The image could not be read."},
    )
    repeated = _initiate(client, headers, task["id"], local_id="android:retry")
    retried = client.post(
        f"/v1/tasks/{task['id']}/assets/initiate",
        headers=headers,
        json={
            "local_asset_id": "android:retry",
            "filename": "IMG.jpg",
            "media_type": "image/jpeg",
            "byte_size": 8,
            "sha256": "c" * 64,
            "retry": True,
        },
    )
    updated = client.get(f"/v1/tasks/{task['id']}", headers=headers)

    assert failed.json()["status"] == "failed"
    assert (repeated.status_code, repeated.json()["provider_upload_id"]) == (201, None)
    assert retried.json()["provider_upload_id"] is not None
    assert (updated.json()["total_assets"], updated.json()["failed_assets"]) == (1, 0)


def test_assets_hide_another_tenants_ids(
    client: TestClient,
    seeded_invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Signing a foreign asset must be indistinguishable from a missing asset."""
    first_headers = _headers(client, seeded_invite, fake_otp)
    task = client.post(
        "/v1/tasks", headers=first_headers, json={"name": "Private"}
    ).json()
    asset_id = _initiate(client, first_headers, task["id"]).json()["asset_id"]

    async def other_token() -> str:
        user = User(phone_e164="+8613800013002")
        async with auth_session_factory() as session:
            session.add(user)
            await session.commit()
        return (
            await _app(client).state.token_service.issue_pair(user.id, "other-device")
        ).access_token

    response = client.post(
        f"/v1/assets/{asset_id}/parts/sign",
        headers={"Authorization": f"Bearer {asyncio.run(other_token())}"},
        json={"part_number": 1},
    )

    assert (response.status_code, response.json()["detail"]) == (404, "asset_not_found")


def test_signed_url_is_not_logged(
    client: TestClient, seeded_invite, fake_otp, caplog
) -> None:
    """Logging the presigned URL would disclose a bearer-style upload credential."""
    headers = _headers(client, seeded_invite, fake_otp)
    task = client.post("/v1/tasks", headers=headers, json={"name": "No secrets"}).json()
    asset_id = _initiate(client, headers, task["id"]).json()["asset_id"]
    response = client.post(
        f"/v1/assets/{asset_id}/parts/sign", headers=headers, json={"part_number": 1}
    )

    assert response.status_code == 200
    assert "X-Amz-Signature" not in caplog.text
    assert "test-access-key" not in caplog.text
