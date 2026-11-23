from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from photo_organizer.config import Settings, async_database_url

settings = Settings()  # type: ignore[call-arg]  # Loaded and validated from environment.
engine = create_async_engine(
    async_database_url(settings.database_url), pool_pre_ping=True
)
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)
