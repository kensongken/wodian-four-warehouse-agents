import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from product_engine.api.dependencies import get_runtime_database, get_runtime_settings
from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.domain.errors import EventConflict, InvalidSignature
from product_engine.repositories.events import EventRepository
from product_engine.repositories.products import ProductRepository
from product_engine.repositories.sales_strategy_inputs import SalesStrategyInputRepository
from product_engine.security.signatures import verify_request_signature
from product_engine.services.album_ingestion import AlbumIngestionService, IngestionResult
from product_engine.services.material_classification import classify_album_category

Clock = Callable[[], int]
Secret = bytes | Callable[[], bytes]


class AlbumService(Protocol):
    async def ingest(
        self,
        event: ProductImageSetReadyV1,
        payload_sha256: str,
    ) -> IngestionResult: ...


@dataclass(frozen=True)
class AuthenticatedAlbumEvent:
    event: ProductImageSetReadyV1
    payload_sha256: str


def error_response(
    status_code: int,
    error_code: str,
    message_zh: str,
    next_step_zh: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error_code": error_code,
            "message_zh": message_zh,
            "next_step_zh": next_step_zh,
        },
    )


async def authenticate_album_event(
    request: Request,
    secret: bytes,
    now: Clock,
) -> AuthenticatedAlbumEvent | JSONResponse:
    source_system = request.headers.get("X-Source-System")
    tenant_value = request.headers.get("X-Tenant-Id")
    timestamp_value = request.headers.get("X-Signature-Timestamp")
    signature = request.headers.get("X-Signature")
    if source_system != "album" or not tenant_value or not timestamp_value or not signature:
        return error_response(
            401,
            "authentication_failed",
            "相册整理请求缺少有效的系统签名。",
            "请由相册整理系统重新发送请求。",
        )

    try:
        tenant_id = UUID(tenant_value)
        timestamp = int(timestamp_value)
    except (ValueError, TypeError):
        return error_response(
            401,
            "authentication_failed",
            "相册整理请求的认证信息格式无效。",
            "请由相册整理系统重新生成签名后发送。",
        )

    body = await request.body()
    try:
        verify_request_signature(
            secret=secret,
            timestamp=timestamp,
            now=now(),
            tolerance_seconds=300,
            source_system=source_system,
            tenant_id=tenant_id,
            method=request.method,
            path=request.url.path,
            body=body,
            supplied_signature=signature,
        )
    except InvalidSignature:
        return error_response(
            401,
            "authentication_failed",
            "相册整理请求的签名无效或已经过期。",
            "请由相册整理系统使用当前时间重新发送请求。",
        )

    try:
        event = ProductImageSetReadyV1.model_validate_json(body)
    except ValidationError:
        return error_response(
            422,
            "invalid_album_payload",
            "相册整理提交的商品图片资料不符合接口要求。",
            "请检查商品编码、图片格式、时间和置信度后重新发送。",
        )
    if event.tenant_id != tenant_id:
        return error_response(
            401,
            "tenant_binding_failed",
            "请求签名中的商家与商品资料中的商家不一致。",
            "请由相册整理系统为正确商家重新生成签名。",
        )

    expected_prefix = f"tenants/{tenant_id}/"
    if any(not image.object_key.startswith(expected_prefix) for image in event.images):
        return error_response(
            422,
            "object_outside_tenant_prefix",
            "商品图片不属于当前商家的私有图片目录。",
            "请把图片保存到当前商家的目录后重新发送。",
        )
    return AuthenticatedAlbumEvent(event, hashlib.sha256(body).hexdigest())


def build_album_router(
    service: AlbumService,
    secret: Secret,
    *,
    now: Clock = lambda: int(time.time()),
) -> APIRouter:
    router = APIRouter(tags=["相册整理"])

    @router.post(
        "/v1/integrations/album/product-image-sets",
        summary="接收已编码的商品图片集",
        description="验证相册整理签名和商家归属，防重复接收并生成原始销售素材包。",
        response_model=None,
    )
    async def ingest(request: Request) -> JSONResponse:
        resolved_secret = secret() if callable(secret) else secret
        authenticated = await authenticate_album_event(request, resolved_secret, now)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        try:
            result = await service.ingest(
                authenticated.event,
                authenticated.payload_sha256,
            )
        except EventConflict:
            return error_response(
                409,
                "event_conflict",
                "同一个事件编号被用于不同的商品资料。",
                "请生成新的事件编号，或重发原始未修改的资料。",
            )
        status_code = 202 if result.disposition == "accepted" else 200
        classification_decision = classify_album_category(
            authenticated.event.category,
            authenticated.event.classification_confidence,
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "event_id": str(result.event_id),
                "product_code": result.product_code,
                "package_version": result.package_version,
                "disposition": result.disposition,
                "classification_decision": classification_decision.model_dump(mode="json"),
            },
        )

    return router


class ProductionAlbumService:
    async def ingest(
        self,
        event: ProductImageSetReadyV1,
        payload_sha256: str,
    ) -> IngestionResult:
        async with get_runtime_database().session() as session:
            service = AlbumIngestionService(
                EventRepository(session),
                ProductRepository(session),
                SalesStrategyInputRepository(session),
            )
            return await service.ingest(event, payload_sha256)


def album_signing_secret() -> bytes:
    return get_runtime_settings().album_signing_secret.get_secret_value().encode()


album_router = build_album_router(ProductionAlbumService(), album_signing_secret)
