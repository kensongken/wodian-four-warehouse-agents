"""Invitation-gated OTP authentication workflows."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.auth.otp_sender import OtpSender
from photo_organizer.auth.token_service import TokenPair, TokenService
from photo_organizer.models.auth import Invite, OtpChallenge
from photo_organizer.models.user import User
from photo_organizer.repositories.unit_of_work import UnitOfWork

OTP_LIFETIME = timedelta(minutes=5)
MAX_OTP_ATTEMPTS = 5
IP_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


class InvalidInviteError(Exception):
    """Raised when an invite cannot authorize an OTP challenge."""


class InvalidOtpError(Exception):
    """Raised when an OTP is missing, expired, exhausted, or wrong."""


class RateLimitedError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds


class OtpDeliveryError(Exception):
    """Raised after an OTP provider fails to accept a challenge."""


class AuthService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        otp_sender: OtpSender,
        token_service: TokenService,
        otp_pepper: str,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._otp_sender = otp_sender
        self._token_service = token_service
        self._otp_pepper = otp_pepper.encode("utf-8")

    async def request_code(
        self, phone_e164: str, invite_code: str, client_ip: str
    ) -> None:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            invite = await unit_of_work.session.scalar(
                select(Invite).where(Invite.code == invite_code).with_for_update()
            )
            now = datetime.now(UTC)
            if (
                invite is None
                or invite.redeemed_at is not None
                or (invite.phone_e164 is not None and invite.phone_e164 != phone_e164)
                or (invite.expires_at is not None and invite.expires_at <= now)
            ):
                raise InvalidInviteError
            await self._enforce_request_limits(phone_e164, client_ip)
            challenge_id = uuid4()
            code = f"{secrets.randbelow(1_000_000):06d}"
            unit_of_work.session.add(
                OtpChallenge(
                    id=challenge_id,
                    phone_e164=phone_e164,
                    invite_id=invite.id,
                    code_hash=self._hash_otp(challenge_id, code),
                    expires_at=now + OTP_LIFETIME,
                )
            )
        try:
            await self._otp_sender.send_code(phone_e164, code)
        except Exception as error:
            raise OtpDeliveryError from error
        async with UnitOfWork(self._session_factory) as unit_of_work:
            challenge = await unit_of_work.session.scalar(
                select(OtpChallenge)
                .where(OtpChallenge.id == challenge_id)
                .with_for_update()
            )
            if challenge is None:
                raise RuntimeError("OTP challenge disappeared before delivery")
            challenge.delivered_at = datetime.now(UTC)

    async def verify_code(
        self, phone_e164: str, code: str, device_id: str
    ) -> TokenPair:
        invalid = False
        async with UnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            challenge = await session.scalar(
                select(OtpChallenge)
                .where(OtpChallenge.phone_e164 == phone_e164)
                .order_by(OtpChallenge.created_at.desc())
                .with_for_update()
            )
            now = datetime.now(UTC)
            if (
                challenge is None
                or challenge.consumed_at is not None
                or challenge.delivered_at is None
                or challenge.expires_at <= now
                or challenge.attempt_count >= MAX_OTP_ATTEMPTS
            ):
                invalid = True
            else:
                challenge.attempt_count += 1
                if not hmac.compare_digest(
                    challenge.code_hash, self._hash_otp(challenge.id, code)
                ):
                    invalid = True
                else:
                    invite = await session.scalar(
                        select(Invite)
                        .where(Invite.id == challenge.invite_id)
                        .with_for_update()
                    )
                    if invite is None or invite.redeemed_at is not None:
                        invalid = True
                    else:
                        user = await self._get_or_create_user(session, phone_e164)
                        challenge.consumed_at = now
                        invite.redeemed_at = now
                        invite.redeemed_by_user_id = user.id
                        return await self._token_service._issue_pair_in_session(
                            session, user.id, device_id
                        )
        if invalid:
            raise InvalidOtpError
        raise RuntimeError("unreachable OTP verification state")

    async def refresh(self, refresh_token: str, device_id: str) -> TokenPair:
        return await self._token_service.rotate_pair(refresh_token, device_id)

    async def _enforce_request_limits(self, phone_e164: str, client_ip: str) -> None:
        phone_key = f"auth:otp:phone:{phone_e164}"
        if not await self._redis.set(phone_key, "1", ex=60, nx=True):
            ttl = await self._redis.ttl(phone_key)
            raise RateLimitedError(max(ttl, 1))

        ip_key = f"auth:otp:ip:{client_ip}"
        result = await self._redis.eval(IP_RATE_LIMIT_SCRIPT, 1, ip_key, 3600)
        requests = int(result[0])
        ttl = int(result[1])
        if requests > 10:
            raise RateLimitedError(max(ttl, 1))

    async def _get_or_create_user(self, session: AsyncSession, phone_e164: str) -> User:
        await session.execute(
            insert(User)
            .values(phone_e164=phone_e164)
            .on_conflict_do_nothing(index_elements=[User.phone_e164])
        )
        user = await session.scalar(select(User).where(User.phone_e164 == phone_e164))
        if user is None:
            raise RuntimeError("canonical user creation did not return a user")
        return user

    def _hash_otp(self, challenge_id: UUID, code: str) -> str:
        payload = f"{challenge_id}{code}".encode()
        return hmac.new(self._otp_pepper, payload, hashlib.sha256).hexdigest()
