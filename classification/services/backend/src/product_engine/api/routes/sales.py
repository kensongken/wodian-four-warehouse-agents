import hashlib
import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from product_engine.api.routes.album import error_response
from product_engine.contracts.sales import SalesMaterialUsageV1
from product_engine.domain.errors import (
    EventConflict,
    InvalidSignature,
    MaterialUsageReferenceNotFound,
)
from product_engine.security.signatures import verify_request_signature
from product_engine.services.sales_usage import UsageResult

Clock = Callable[[], int]
Secret = bytes | Callable[[], bytes]


class SalesUsageService(Protocol):
    async def record(self, event: SalesMaterialUsageV1, payload_sha256: str) -> "UsageResult": ...


def build_sales_router(
    service: SalesUsageService, secret: Secret, *, now: Clock = lambda: int(time.time())
) -> APIRouter:
    router = APIRouter(tags=["销售素材使用"])

    @router.post("/v1/integrations/sales/material-usage", response_model=None)
    async def record_usage(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            tenant_id = UUID(request.headers["X-Tenant-Id"])
            verify_request_signature(
                secret=secret() if callable(secret) else secret,
                timestamp=int(request.headers["X-Signature-Timestamp"]),
                now=now(),
                tolerance_seconds=300,
                source_system="sales",
                tenant_id=tenant_id,
                method="POST",
                path=request.url.path,
                body=body,
                supplied_signature=request.headers["X-Signature"],
            )
            if request.headers.get("X-Source-System") != "sales":
                raise InvalidSignature("source")
        except (KeyError, ValueError, InvalidSignature):
            return error_response(
                401,
                "authentication_failed",
                "销售之旅请求的签名无效或已经过期。",
                "请由销售之旅重新发送请求。",
            )
        try:
            event = SalesMaterialUsageV1.model_validate_json(body)
        except ValidationError:
            return error_response(
                422,
                "invalid_sales_usage",
                "销售素材使用资料不符合接口要求。",
                "请检查使用类型和素材编号后重新发送。",
            )
        if event.tenant_id != tenant_id:
            return error_response(
                401,
                "tenant_binding_failed",
                "签名商家与使用资料不一致。",
                "请使用正确商家的签名重新发送。",
            )
        try:
            result = await service.record(event, hashlib.sha256(body).hexdigest())
        except EventConflict:
            return error_response(
                409, "event_conflict", "同一事件编号不能对应不同资料。", "请生成新的事件编号。"
            )
        except MaterialUsageReferenceNotFound as error:
            return error_response(
                404,
                str(error),
                "素材并不属于该商品指定版本的销售素材包。",
                "请读取最新素材包后重新提交使用记录。",
            )
        return JSONResponse(
            status_code=202 if result.disposition == "accepted" else 200,
            content={"event_id": str(event.event_id), "disposition": result.disposition},
        )

    return router


class ProductionSalesUsageService:
    async def record(self, event: SalesMaterialUsageV1, payload_sha256: str) -> UsageResult:
        from product_engine.api.dependencies import get_runtime_database
        from product_engine.repositories.events import EventRepository
        from product_engine.repositories.usage import UsageRepository
        from product_engine.services.sales_usage import SalesUsageService

        async with get_runtime_database().session() as session:
            return await SalesUsageService(
                store=UsageRepository(session), inbox=EventRepository(session)
            ).record(event, payload_sha256)
