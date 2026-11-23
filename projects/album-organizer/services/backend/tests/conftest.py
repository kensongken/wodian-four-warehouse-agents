import os
from collections.abc import AsyncIterator
from time import monotonic

os.environ.update(
    {
        "APP_ENV": "test",
        "S3_ACCESS_KEY_ID": "test-access-key",
        "S3_SECRET_ACCESS_KEY": "test-secret-key",
        "JWT_SIGNING_KEY": "test-jwt-signing-key-at-least-32-bytes",
        "OTP_PEPPER": "test-otp-pepper",
        "OTP_SENDER": "tencent",
        "TENCENT_SECRET_ID": "test-secret-id",
        "TENCENT_SECRET_KEY": "test-secret-key",
        "TENCENT_SMS_SDK_APP_ID": "test-app-id",
        "TENCENT_SMS_SIGN_NAME": "test-sign-name",
        "TENCENT_SMS_TEMPLATE_ID": "test-template-id",
    }
)

import pytest
import pytest_asyncio
from fakes.fake_object_storage import FakeObjectStorage
from fakes.fake_otp_sender import FakeOtpSender
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from photo_organizer.api.main import create_app
from photo_organizer.config import async_database_url
from photo_organizer.models.auth import Invite
from photo_organizer.repositories.unit_of_work import UnitOfWork


@pytest.fixture
def get_session_override(db_session: AsyncSession):
    async def override() -> AsyncIterator[AsyncSession]:
        yield db_session

    return override


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    database_url = os.getenv("PHOTO_ORGANIZER_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip(
            "PHOTO_ORGANIZER_TEST_DATABASE_URL is required for PostgreSQL integration tests"
        )

    engine = create_async_engine(async_database_url(database_url), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = async_sessionmaker(
            connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )()
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


class FakeRedis:
    """Small Redis semantic fake for the rate-limit operations used by auth."""

    def __init__(self) -> None:
        self._values: dict[str, int] = {}
        self._expires_at: dict[str, float] = {}

    def _remove_if_expired(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and expires_at <= monotonic():
            self._values.pop(key, None)
            self._expires_at.pop(key, None)

    def clear(self) -> None:
        self._values.clear()
        self._expires_at.clear()

    def prime_without_expiry(self, key: str, value: int) -> None:
        self._values[key] = value

    async def set(self, key: str, value: str, ex: int, nx: bool) -> bool:
        self._remove_if_expired(key)
        if nx and key in self._values:
            return False
        self._values[key] = int(value)
        self._expires_at[key] = monotonic() + ex
        return True

    async def ttl(self, key: str) -> int:
        self._remove_if_expired(key)
        expires_at = self._expires_at.get(key)
        if expires_at is None:
            return -2
        return max(0, int(expires_at - monotonic()))

    async def incr(self, key: str) -> int:
        self._remove_if_expired(key)
        value = self._values.get(key, 0) + 1
        self._values[key] = value
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        self._remove_if_expired(key)
        if key not in self._values:
            return False
        self._expires_at[key] = monotonic() + seconds
        return True

    async def eval(self, script: str, numkeys: int, key: str, expiry: int) -> list[int]:
        del script
        if numkeys != 1:
            raise ValueError("only one Redis key is supported")
        self._remove_if_expired(key)
        count = self._values.get(key, 0) + 1
        self._values[key] = count
        if key not in self._expires_at:
            self._expires_at[key] = monotonic() + expiry
        return [count, await self.ttl(key)]


@pytest_asyncio.fixture
async def auth_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("PHOTO_ORGANIZER_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("PHOTO_ORGANIZER_TEST_DATABASE_URL is required for auth tests")
    engine = create_async_engine(
        async_database_url(database_url), poolclass=NullPool, pool_pre_ping=True
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text("TRUNCATE refresh_tokens, otp_challenges, invites, users CASCADE")
        )
        await session.commit()
    try:
        yield factory
    finally:
        async with factory() as session:
            await session.execute(
                text("TRUNCATE refresh_tokens, otp_challenges, invites, users CASCADE")
            )
            await session.commit()
        await engine.dispose()


@pytest.fixture
def fake_otp() -> FakeOtpSender:
    return FakeOtpSender()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest_asyncio.fixture
async def seeded_invite(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> Invite:
    invite = Invite(code="PILOT-0001")
    async with UnitOfWork(auth_session_factory) as unit_of_work:
        unit_of_work.session.add(invite)
        await unit_of_work.session.flush()
    return invite


@pytest.fixture
def client(
    auth_session_factory: async_sessionmaker[AsyncSession],
    fake_otp: FakeOtpSender,
    fake_redis: FakeRedis,
):
    app = create_app(
        session_factory=auth_session_factory,
        redis_client=fake_redis,  # type: ignore[arg-type]
        otp_sender=fake_otp,
        object_storage=FakeObjectStorage(),
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client
