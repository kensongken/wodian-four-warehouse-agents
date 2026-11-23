from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis import asyncio as redis_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.api.routes.assets import router as assets_router
from photo_organizer.api.routes.auth import router as auth_router
from photo_organizer.api.routes.classification_reviews import (
    router as classification_reviews_router,
)
from photo_organizer.api.routes.evidence_assets import router as evidence_assets_router
from photo_organizer.api.routes.group_confirmation import (
    router as group_confirmation_router,
)
from photo_organizer.api.routes.health import DependencyProbe, build_health_router
from photo_organizer.api.routes.me import router as me_router
from photo_organizer.api.routes.product_material_download import (
    router as product_material_download_router,
)
from photo_organizer.api.routes.product_material_manifest import (
    router as product_material_manifest_router,
)
from photo_organizer.api.routes.tasks import router as tasks_router
from photo_organizer.api.routes.upload_bindings import router as upload_bindings_router
from photo_organizer.auth.otp_sender import (
    DevelopmentConsoleOtpSender,
    OtpSender,
    TencentCloudOtpSender,
)
from photo_organizer.auth.token_service import TokenService
from photo_organizer.catalog.downloads import ProductMaterialDownloadService
from photo_organizer.catalog.evidence_assets import EvidenceAssetRegistrationService
from photo_organizer.catalog.group_confirmation import (
    AlbumProductGroupConfirmationService,
)
from photo_organizer.catalog.upload_bindings import UploadBindingService
from photo_organizer.config import Settings
from photo_organizer.db import async_session_factory
from photo_organizer.services.auth_service import AuthService
from photo_organizer.services.upload_service import UploadService
from photo_organizer.storage.object_storage import ObjectStorage
from photo_organizer.storage.s3_storage import S3Storage

ReadinessProbe = Callable[[], Awaitable[bool]]


def create_app(
    readiness_probe: ReadinessProbe | None = None,
    *,
    settings_override: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: redis_asyncio.Redis | None = None,
    otp_sender: OtpSender | None = None,
    object_storage: ObjectStorage | None = None,
) -> FastAPI:
    settings = settings_override or Settings()  # type: ignore[call-arg]  # Loaded and validated from environment.
    probe = readiness_probe or DependencyProbe(settings)
    database_sessions = session_factory or async_session_factory
    sender = otp_sender or _build_otp_sender(settings)
    token_service = TokenService(
        database_sessions, settings.jwt_signing_key.get_secret_value()
    )
    storage_adapter = object_storage or S3Storage(
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        bucket=settings.s3_bucket,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )
    app = FastAPI(title="Photo Organizer API", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def completion_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        if (
            request.url.path.endswith("/complete")
            and "/v1/assets/" in request.url.path
            and any(
                issue.get("loc", ())[:2] == ("body", "parts")
                for issue in error.errors()
            )
        ):
            return JSONResponse(status_code=422, content={"detail": "invalid_parts"})
        if request.method == "POST" and request.url.path.endswith("/upload-bindings"):
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid_candidate"},
            )
        return JSONResponse(
            status_code=422, content={"detail": jsonable_encoder(error.errors())}
        )

    app.state.session_factory = database_sessions
    app.state.token_service = token_service
    app.state.auth_service = AuthService(
        database_sessions,
        redis_client or redis_asyncio.from_url(settings.redis_url),
        sender,
        token_service,
        settings.otp_pepper.get_secret_value(),
    )
    app.state.upload_service = UploadService(
        database_sessions,
        storage_adapter,
    )
    app.state.evidence_asset_service = EvidenceAssetRegistrationService(
        database_sessions,
    )
    app.state.group_confirmation_service = AlbumProductGroupConfirmationService(
        database_sessions,
    )
    app.state.upload_binding_service = UploadBindingService(database_sessions)
    app.state.product_material_download_service = ProductMaterialDownloadService(
        database_sessions,
        storage_adapter,
    )
    app.include_router(build_health_router(probe))
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(tasks_router)
    app.include_router(assets_router)
    app.include_router(evidence_assets_router)
    app.include_router(group_confirmation_router)
    app.include_router(classification_reviews_router)
    app.include_router(product_material_manifest_router)
    app.include_router(product_material_download_router)
    app.include_router(upload_bindings_router)
    return app


def _build_otp_sender(settings: Settings) -> OtpSender:
    if settings.otp_sender == "console":
        return DevelopmentConsoleOtpSender(settings)
    return TencentCloudOtpSender(settings)


app = create_app()
