import asyncio
import logging
from collections.abc import Awaitable, Callable

import asyncpg
import boto3
from fastapi import APIRouter, HTTPException, status
from redis import asyncio as redis_asyncio

from photo_organizer.config import Settings

DependencyProbeCallable = Callable[[], Awaitable[bool]]
logger = logging.getLogger(__name__)


class DependencyProbe:
    """Checks the infrastructure required to accept background work."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(self) -> bool:
        checks = (
            ("postgres", self._check_postgres()),
            ("redis", self._check_redis()),
            ("object_storage", self._check_object_storage()),
        )
        results = await asyncio.gather(
            *(self._run_check(name, check) for name, check in checks)
        )
        return all(results)

    async def _run_check(self, name: str, check: Awaitable[None]) -> bool:
        try:
            await check
        except Exception as exc:  # noqa: BLE001 - readiness treats any dependency error as unavailable.
            logger.warning("readiness check failed: %s (%s)", name, type(exc).__name__)
            return False
        return True

    async def _check_postgres(self) -> None:
        connection = await asyncpg.connect(self._settings.database_url)
        try:
            await connection.execute("SELECT 1")
        finally:
            await connection.close()

    async def _check_redis(self) -> None:
        client = redis_asyncio.from_url(self._settings.redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()

    async def _check_object_storage(self) -> None:
        client = boto3.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint_url,
            region_name=self._settings.s3_region,
            aws_access_key_id=self._settings.s3_access_key_id,
            aws_secret_access_key=self._settings.s3_secret_access_key,
        )
        await asyncio.to_thread(client.head_bucket, Bucket=self._settings.s3_bucket)


def build_health_router(readiness_probe: DependencyProbeCallable) -> APIRouter:
    router = APIRouter(prefix="/health", tags=["health"])

    @router.get("/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/ready")
    async def ready() -> dict[str, str]:
        if not await readiness_probe():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="dependencies_unavailable",
            )
        return {"status": "ready"}

    return router
