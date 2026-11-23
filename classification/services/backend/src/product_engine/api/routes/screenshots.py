import hashlib
import time
from collections.abc import Callable
from typing import Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from product_engine.api.routes.album import error_response
from product_engine.api.routes.materials import authenticated_sales_tenant
from product_engine.contracts.screenshots import ScreenshotImportedV1
from product_engine.domain.errors import (
    EventConflict,
    MaterialUsageReferenceNotFound,
    ObjectValidationFailed,
)
from product_engine.services.screenshot_imports import (
    ScreenshotImportResult,
    ScreenshotImportService,
)
from product_engine.storage.protocol import PresignedUpload

Clock = Callable[[], int]
Secret = bytes | Callable[[], bytes]
MimeType = Literal["image/heic", "image/jpeg", "image/png"]


class ScreenshotUploadRequest(BaseModel):
    mime_type: MimeType
    byte_size: int = Field(ge=1, le=20_971_520)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScreenshotService(Protocol):
    async def create_upload(
        self,
        *,
        tenant_id: UUID,
        mime_type: str,
        byte_size: int,
        sha256: str,
    ) -> PresignedUpload: ...

    async def finalize(
        self,
        event: ScreenshotImportedV1,
        payload_sha256: str,
    ) -> ScreenshotImportResult: ...


def build_screenshots_router(
    service: ScreenshotService,
    secret: Secret,
    *,
    now: Clock = lambda: int(time.time()),
) -> APIRouter:
    router = APIRouter(tags=["交易截图"])

    @router.post("/v1/integrations/sales/screenshots/uploads", status_code=201)
    async def create_upload(request: Request) -> JSONResponse:
        resolved_secret = secret() if callable(secret) else secret
        tenant_id = await authenticated_sales_tenant(request, resolved_secret, now)
        if isinstance(tenant_id, JSONResponse):
            return tenant_id
        try:
            payload = ScreenshotUploadRequest.model_validate_json(await request.body())
        except ValidationError:
            return error_response(
                422,
                "invalid_screenshot_upload",
                "交易截图的格式、大小或摘要无效。",
                "请上传不超过 20 MiB 的 HEIC、JPEG 或 PNG 图片。",
            )
        upload = await service.create_upload(tenant_id=tenant_id, **payload.model_dump())
        return JSONResponse(
            status_code=201,
            content={
                "object_key": upload.object_key,
                "url": upload.url,
                "headers": upload.headers,
                "expires_in_seconds": upload.expires_in_seconds,
            },
        )

    @router.post("/v1/integrations/sales/screenshots/imports")
    async def finalize(request: Request) -> JSONResponse:
        resolved_secret = secret() if callable(secret) else secret
        tenant_id = await authenticated_sales_tenant(request, resolved_secret, now)
        if isinstance(tenant_id, JSONResponse):
            return tenant_id
        body = await request.body()
        try:
            event = ScreenshotImportedV1.model_validate_json(body)
        except ValidationError:
            return error_response(
                422,
                "invalid_screenshot_import",
                "交易截图导入资料不符合接口要求。",
                "请检查截图编号、图片资料和摘要后重试。",
            )
        if event.tenant_id != tenant_id:
            return error_response(
                401,
                "tenant_binding_failed",
                "签名商家与截图资料不一致。",
                "请使用正确商家的签名重新发送。",
            )
        try:
            result = await service.finalize(event, hashlib.sha256(body).hexdigest())
        except EventConflict:
            return error_response(
                409, "event_conflict", "事件编号已被其他资料使用。", "请生成新事件。"
            )
        except ObjectValidationFailed as error:
            return error_response(
                422,
                str(error),
                "交易截图对象不存在或与提交资料不一致。",
                "请重新上传图片后重试。",
            )
        except MaterialUsageReferenceNotFound:
            return error_response(
                404,
                "product_not_found",
                "截图引用的商品不属于当前商家。",
                "请使用当前商家的商品编码，或省略可选商品编码。",
            )
        return JSONResponse(
            status_code=202 if result.disposition == "accepted" else 200,
            content={
                "screenshot_import_id": str(result.screenshot_import_id),
                "disposition": result.disposition,
            },
        )

    return router


class ProductionScreenshotService:
    async def create_upload(
        self,
        *,
        tenant_id: UUID,
        mime_type: str,
        byte_size: int,
        sha256: str,
    ) -> PresignedUpload:
        from product_engine.api.routes.templates import production_storage

        return await ScreenshotImportService(production_storage()).create_upload(
            tenant_id=tenant_id,
            mime_type=mime_type,
            byte_size=byte_size,
            sha256=sha256,
        )

    async def finalize(
        self,
        event: ScreenshotImportedV1,
        payload_sha256: str,
    ) -> ScreenshotImportResult:
        from product_engine.api.dependencies import get_runtime_database
        from product_engine.api.routes.templates import production_storage
        from product_engine.repositories.events import EventRepository
        from product_engine.repositories.products import ProductRepository
        from product_engine.repositories.screenshots import ScreenshotRepository

        async with get_runtime_database().session() as session:
            events = EventRepository(session)
            return await ScreenshotImportService(
                production_storage(),
                store=ScreenshotRepository(session),
                inbox=events,
                outbox=events,
                products=ProductRepository(session),
            ).finalize(event, payload_sha256)
