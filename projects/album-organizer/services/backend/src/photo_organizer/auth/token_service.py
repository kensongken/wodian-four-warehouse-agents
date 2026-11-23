"""Access-token issuance and durable refresh-token rotation."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.models.auth import RefreshToken
from photo_organizer.repositories.unit_of_work import UnitOfWork

ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)


class InvalidRefreshTokenError(Exception):
    """Raised when a refresh token cannot be used."""


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in_seconds: int = 900


class TokenService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        jwt_signing_key: str,
    ) -> None:
        self._session_factory = session_factory
        self._jwt_signing_key = jwt_signing_key

    async def issue_pair(self, user_id: UUID, device_id: str) -> TokenPair:
        async with UnitOfWork(self._session_factory) as unit_of_work:
            return await self._issue_pair_in_session(
                unit_of_work.session, user_id, device_id
            )

    async def rotate_pair(self, refresh_token: str, device_id: str) -> TokenPair:
        now = datetime.now(UTC)
        token_hash = self.hash_refresh_token(refresh_token)
        invalid = False
        async with UnitOfWork(self._session_factory) as unit_of_work:
            session = unit_of_work.session
            stored = await session.scalar(
                select(RefreshToken)
                .where(RefreshToken.token_hash == token_hash)
                .with_for_update()
            )
            if stored is None:
                invalid = True
            elif stored.revoked_at is not None or stored.replaced_by_id is not None:
                await session.execute(
                    update(RefreshToken)
                    .where(
                        RefreshToken.user_id == stored.user_id,
                        RefreshToken.device_id == stored.device_id,
                        RefreshToken.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                invalid = True
            elif stored.device_id != device_id:
                invalid = True
            elif stored.expires_at <= now:
                stored.revoked_at = now
                invalid = True
            else:
                replacement = await self._issue_pair_in_session(
                    session, stored.user_id, stored.device_id
                )
                replacement_hash = self.hash_refresh_token(replacement.refresh_token)
                stored.replaced_by_id = await session.scalar(
                    select(RefreshToken.id).where(
                        RefreshToken.token_hash == replacement_hash
                    )
                )
                stored.revoked_at = now
                return replacement
        if invalid:
            raise InvalidRefreshTokenError
        raise RuntimeError("unreachable refresh token state")

    def decode_access_token(self, token: str) -> dict[str, object]:
        try:
            claims = jwt.decode(
                token,
                self._jwt_signing_key,
                algorithms=["HS256"],
                options={"require": ["sub", "device_id", "iat", "exp", "jti"]},
            )
        except jwt.PyJWTError as error:
            raise ValueError("invalid access token") from error
        required = {"sub", "device_id", "iat", "exp", "jti"}
        if set(claims) != required:
            raise ValueError("invalid access token")
        return claims

    @staticmethod
    def hash_refresh_token(refresh_token: str) -> str:
        return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()

    async def _issue_pair_in_session(
        self, session: AsyncSession, user_id: UUID, device_id: str
    ) -> TokenPair:
        now = datetime.now(UTC)
        access_token = jwt.encode(
            {
                "sub": str(user_id),
                "device_id": device_id,
                "iat": int(now.timestamp()),
                "exp": int((now + ACCESS_TOKEN_LIFETIME).timestamp()),
                "jti": str(uuid4()),
            },
            self._jwt_signing_key,
            algorithm="HS256",
        )
        refresh_token = secrets.token_urlsafe(32)
        session.add(
            RefreshToken(
                user_id=user_id,
                device_id=device_id,
                token_hash=self.hash_refresh_token(refresh_token),
                expires_at=now + REFRESH_TOKEN_LIFETIME,
            )
        )
        await session.flush()
        return TokenPair(access_token=access_token, refresh_token=refresh_token)
