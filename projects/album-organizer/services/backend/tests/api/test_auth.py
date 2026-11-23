import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.api.dependencies import get_current_user
from photo_organizer.api.main import create_app
from photo_organizer.config import Settings
from photo_organizer.models.auth import Invite, OtpChallenge, RefreshToken
from photo_organizer.models.user import User


def test_request_code_rejects_unknown_invite(client: TestClient) -> None:
    """Rejecting an unknown invite must fail if invite validation is removed."""
    response = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": "INVALID"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_invite"


def test_verify_code_returns_access_and_rotating_refresh_token(
    client: TestClient, seeded_invite: Invite, fake_otp
) -> None:
    """Removing invitation redemption or token issuance must fail this login flow."""
    request = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": seeded_invite.code},
    )
    code = fake_otp.last_code_for("+8613800000000")
    response = client.post(
        "/v1/auth/verify-code",
        json={"phone_e164": "+8613800000000", "code": code, "device_id": "ios-test"},
    )

    assert request.status_code == 202
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["refresh_token"]


def test_expired_otp_is_rejected(
    client: TestClient, seeded_invite: Invite, fake_otp
) -> None:
    """Accepting an OTP after its five-minute deadline must fail this test."""
    client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": seeded_invite.code},
    )
    code = fake_otp.last_code_for("+8613800000000")
    auth_service = client.app.state.auth_service

    # A direct persistence update makes expiry deterministic without changing the clock.
    async def expire_challenge() -> None:
        async with auth_service._session_factory() as session:
            challenge = await session.scalar(select(OtpChallenge))
            assert challenge is not None
            challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire_challenge())
    response = client.post(
        "/v1/auth/verify-code",
        json={"phone_e164": "+8613800000000", "code": code, "device_id": "ios-test"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_otp"


def test_sixth_failed_attempt_is_rejected(
    client: TestClient, seeded_invite: Invite, fake_otp
) -> None:
    """Allowing a sixth OTP guess must fail this attempt-limit test."""
    client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": seeded_invite.code},
    )
    code = fake_otp.last_code_for("+8613800000000")
    payload = {
        "phone_e164": "+8613800000000",
        "code": "000000",
        "device_id": "ios-test",
    }
    for _ in range(5):
        assert client.post("/v1/auth/verify-code", json=payload).status_code == 401

    response = client.post(
        "/v1/auth/verify-code",
        json={"phone_e164": "+8613800000000", "code": code, "device_id": "ios-test"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_otp"


def test_request_code_throttles_phone_and_ip(
    client: TestClient,
    seeded_invite: Invite,
    auth_session_factory: async_sessionmaker[AsyncSession],
    fake_redis,
) -> None:
    """Removing either Redis throttle must fail this test."""
    payload = {"phone_e164": "+8613800000000", "invite_code": seeded_invite.code}
    assert client.post("/v1/auth/request-code", json=payload).status_code == 202
    response = client.post("/v1/auth/request-code", json=payload)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    fake_redis.clear()

    async def create_ip_test_invites() -> None:
        async with auth_session_factory() as session:
            session.add_all([Invite(code=f"IP-TEST-{number}") for number in range(11)])
            await session.commit()

    asyncio.run(create_ip_test_invites())
    for number in range(10):
        response = client.post(
            "/v1/auth/request-code",
            json={
                "phone_e164": f"+861380000{number:04d}",
                "invite_code": f"IP-TEST-{number}",
            },
        )
        assert response.status_code == 202
    response = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000010", "invite_code": "IP-TEST-10"},
    )
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_refresh_rotates_and_replay_revokes_device_family(
    client: TestClient, seeded_invite: Invite, fake_otp
) -> None:
    """A reused refresh must invalidate the rotated replacement too."""
    client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": seeded_invite.code},
    )
    login = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": "+8613800000000",
            "code": fake_otp.last_code_for("+8613800000000"),
            "device_id": "ios-test",
        },
    ).json()
    rotated = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"], "device_id": "ios-test"},
    )
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != login["refresh_token"]

    replay = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"], "device_id": "ios-test"},
    )
    assert replay.status_code == 401
    replacement_reuse = client.post(
        "/v1/auth/refresh",
        json={
            "refresh_token": rotated.json()["refresh_token"],
            "device_id": "ios-test",
        },
    )
    assert replacement_reuse.status_code == 401


def test_wrong_device_refresh_replay_revokes_stored_device_family(
    client: TestClient, seeded_invite: Invite, fake_otp
) -> None:
    """A replay must revoke its stored family even when its supplied device differs."""
    client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": seeded_invite.code},
    )
    login = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": "+8613800000000",
            "code": fake_otp.last_code_for("+8613800000000"),
            "device_id": "ios-test",
        },
    ).json()
    rotated = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"], "device_id": "ios-test"},
    ).json()

    replay = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"], "device_id": "android-test"},
    )
    assert replay.status_code == 401
    replacement = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": rotated["refresh_token"], "device_id": "ios-test"},
    )
    assert replacement.status_code == 401


def test_expired_refresh_token_is_rejected(
    client: TestClient,
    seeded_invite: Invite,
    fake_otp,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Accepting a refresh token after its 30-day lifetime must fail this test."""
    client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": seeded_invite.code},
    )
    login = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": "+8613800000000",
            "code": fake_otp.last_code_for("+8613800000000"),
            "device_id": "ios-test",
        },
    ).json()

    async def expire_refresh_token() -> None:
        async with auth_session_factory() as session:
            refresh = await session.scalar(select(RefreshToken))
            assert refresh is not None
            refresh.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    asyncio.run(expire_refresh_token())
    response = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"], "device_id": "ios-test"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_refresh_token"


def test_single_use_invite_is_consumed_after_successful_verification(
    client: TestClient, seeded_invite: Invite, fake_otp
) -> None:
    """Leaving an invite reusable after login must fail this test."""
    payload = {"phone_e164": "+8613800000000", "invite_code": seeded_invite.code}
    client.post("/v1/auth/request-code", json=payload)
    client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": "+8613800000000",
            "code": fake_otp.last_code_for("+8613800000000"),
            "device_id": "ios-test",
        },
    )
    response = client.post("/v1/auth/request-code", json=payload)
    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_invite"


def test_ip_limit_recovers_a_counter_missing_its_ttl(
    client: TestClient,
    fake_redis,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An interrupted first increment must not leave a permanent IP counter."""

    async def create_invites() -> None:
        async with auth_session_factory() as session:
            session.add_all([Invite(code=f"RECOVER-{number}") for number in range(2)])
            await session.commit()

    asyncio.run(create_invites())
    fake_redis.prime_without_expiry("auth:otp:ip:testclient", 9)
    allowed = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000001", "invite_code": "RECOVER-0"},
    )
    assert allowed.status_code == 202
    rejected = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000002", "invite_code": "RECOVER-1"},
    )
    assert rejected.status_code == 429
    assert int(rejected.headers["Retry-After"]) > 3000


def test_concurrent_canonical_user_creation_uses_one_user(
    client: TestClient, auth_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Concurrent first-user creation must converge without an integrity error."""
    auth_service = client.app.state.auth_service

    async def create_user() -> User:
        async with auth_session_factory() as session, session.begin():
            return await auth_service._get_or_create_user(session, "+8613800000000")

    async def run_concurrently() -> tuple[User, User, int]:
        first, second = await asyncio.gather(create_user(), create_user())
        async with auth_session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.phone_e164 == "+8613800000000")
            )
            assert count is not None
        return first, second, count

    first, second, count = asyncio.run(run_concurrently())
    assert first.id == second.id
    assert count == 1


def test_production_rejects_low_entropy_authentication_secrets() -> None:
    """Weak production JWT and OTP secrets must fail startup validation."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="secure authentication secrets"):
        Settings(
            app_env="production",
            s3_access_key_id="production-access-key",
            s3_secret_access_key="production-secret-key",
            jwt_signing_key="j" * 32,
            otp_pepper="p" * 32,
            otp_sender="tencent",
            tencent_secret_id="tencent-secret-id",
            tencent_secret_key="tencent-secret-key",
            tencent_sms_sdk_app_id="sms-app-id",
            tencent_sms_sign_name="sign-name",
            tencent_sms_template_id="template-id",
        )


def test_invalid_and_oversized_auth_inputs_return_validation_errors(
    client: TestClient,
) -> None:
    """Malformed aliases and oversized auth fields must never reach persistence."""
    malformed_phone = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "13800138000", "invite_code": "PILOT-0001"},
    )
    oversized_invite = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": "x" * 129},
    )
    oversized_refresh = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": "x" * 257, "device_id": "ios-test"},
    )
    short_refresh_device = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": "token", "device_id": "short"},
    )

    assert [
        malformed_phone.status_code,
        oversized_invite.status_code,
        oversized_refresh.status_code,
        short_refresh_device.status_code,
    ] == [422, 422, 422, 422]


def test_leading_zero_country_code_is_rejected_before_auth_service(
    client: TestClient,
) -> None:
    """A non-canonical +00 phone number must be rejected at request validation."""
    response = client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+0012345678", "invite_code": "PILOT-0001"},
    )

    assert response.status_code == 422


class FailingOtpSender:
    def __init__(self) -> None:
        self.code: str | None = None

    async def send_code(self, phone_e164: str, code: str) -> None:
        del phone_e164
        self.code = code
        raise OSError("delivery unavailable")


def test_sender_failure_never_creates_a_verifiable_challenge(
    auth_session_factory: async_sessionmaker[AsyncSession],
    seeded_invite: Invite,
    fake_redis,
) -> None:
    """A challenge from a failed OTP send must be rejected even with its code."""
    sender = FailingOtpSender()
    app = create_app(
        session_factory=auth_session_factory,
        redis_client=fake_redis,  # type: ignore[arg-type]
        otp_sender=sender,
    )
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        requested = failing_client.post(
            "/v1/auth/request-code",
            json={"phone_e164": "+8613800000000", "invite_code": seeded_invite.code},
        )
        assert requested.status_code == 503
        assert sender.code is not None
        verified = failing_client.post(
            "/v1/auth/verify-code",
            json={
                "phone_e164": "+8613800000000",
                "code": sender.code,
                "device_id": "ios-test",
            },
        )
    assert verified.status_code == 401


def test_missing_bearer_token_is_rejected(client: TestClient) -> None:
    """Removing bearer authentication from a protected endpoint must fail this test."""

    @client.app.get("/_test/protected")
    async def protected(
        user: User = Depends(get_current_user),  # noqa: B008
    ) -> dict[str, str]:
        return {"phone_e164": user.phone_e164}

    response = client.get("/_test/protected")
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_bearer_token"


def test_bearer_token_authenticates_its_user(
    client: TestClient, seeded_invite: Invite, fake_otp
) -> None:
    """Changing a JWT subject or bypassing bearer validation must fail this test."""

    @client.app.get("/_test/current-user")
    async def current_user(
        user: User = Depends(get_current_user),  # noqa: B008
    ) -> dict[str, str]:
        return {"phone_e164": user.phone_e164}

    client.post(
        "/v1/auth/request-code",
        json={"phone_e164": "+8613800000000", "invite_code": seeded_invite.code},
    )
    login = client.post(
        "/v1/auth/verify-code",
        json={
            "phone_e164": "+8613800000000",
            "code": fake_otp.last_code_for("+8613800000000"),
            "device_id": "ios-test",
        },
    ).json()
    response = client.get(
        "/_test/current-user",
        headers={"Authorization": f"Bearer {login['access_token']}"},
    )
    assert response.status_code == 200
    assert response.json() == {"phone_e164": "+8613800000000"}
    claims = client.app.state.token_service.decode_access_token(login["access_token"])
    assert set(claims) == {"sub", "device_id", "iat", "exp", "jti"}
    assert claims["device_id"] == "ios-test"
    assert int(claims["exp"]) - int(claims["iat"]) == 900
