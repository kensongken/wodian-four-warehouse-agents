"""Drain durable object-storage cleanup jobs.

Run with ``python -m photo_organizer.cli.cleanup`` from the backend service
environment.  It performs one bounded pass; schedulers may invoke it again.
"""

import asyncio

from photo_organizer.config import Settings
from photo_organizer.db import async_session_factory
from photo_organizer.services.upload_service import UploadService
from photo_organizer.storage.s3_storage import S3Storage


async def main(service: UploadService | None = None) -> None:
    """Run one explicit bounded drain; suitable for a cron/job invocation."""
    if service is not None:
        await service.process_cleanup_jobs(limit=100)
        return
    settings = Settings()  # type: ignore[call-arg]  # Environment-backed production settings.
    storage = S3Storage(
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        bucket=settings.s3_bucket,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )
    await UploadService(async_session_factory, storage).process_cleanup_jobs(limit=100)


if __name__ == "__main__":
    asyncio.run(main())
