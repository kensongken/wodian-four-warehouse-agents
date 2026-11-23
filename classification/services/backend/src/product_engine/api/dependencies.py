import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache

from product_engine.config import Settings
from product_engine.db import Database

DatabaseCheck = Callable[[], Awaitable[None]]
ObjectStorageCheck = Callable[[], None]

logger = logging.getLogger(__name__)


@lru_cache
def get_runtime_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@lru_cache
def get_runtime_database() -> Database:
    return Database(get_runtime_settings().database_url)


class DependencyProbe:
    def __init__(
        self,
        database_check: DatabaseCheck,
        object_storage_check: ObjectStorageCheck,
    ) -> None:
        self.database_check = database_check
        self.object_storage_check = object_storage_check

    async def __call__(self) -> bool:
        try:
            await self.database_check()
        except Exception as error:
            self._log_failure("database", error)
            return False

        try:
            await asyncio.to_thread(self.object_storage_check)
        except Exception as error:
            self._log_failure("object_storage", error)
            return False
        return True

    @staticmethod
    def _log_failure(dependency: str, error: Exception) -> None:
        logger.warning(
            "依赖服务不可用：%s (%s)",
            dependency,
            type(error).__name__,
        )
