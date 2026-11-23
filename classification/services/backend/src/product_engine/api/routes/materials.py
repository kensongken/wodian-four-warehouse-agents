import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from product_engine.api.routes.album import error_response
from product_engine.contracts.materials import ProductMaterialsUpdatedV1
from product_engine.domain.errors import InvalidSignature
from product_engine.security.signatures import verify_request_signature
from product_engine.services.material_packages import MaterialPackageService

Clock = Callable[[], int]
Secret = bytes | Callable[[], bytes]


class MaterialPackageReader(Protocol):
    async def latest(
        self,
        tenant_id: UUID,
        product_code: str,
    ) -> ProductMaterialsUpdatedV1 | None: ...


async def authenticated_sales_tenant(
    request: Request,
    secret: bytes,
    now: Clock,
) -> UUID | JSONResponse:
    source_system = request.headers.get("X-Source-System")
    tenant_value = request.headers.get("X-Tenant-Id")
    timestamp_value = request.headers.get("X-Signature-Timestamp")
    signature = request.headers.get("X-Signature")
    if source_system != "sales" or not tenant_value or not timestamp_value or not signature:
        return error_response(
            401,
            "authentication_failed",
            "销售之旅请求缺少有效的系统签名。",
            "请由销售之旅系统重新发送请求。",
        )
    try:
        tenant_id = UUID(tenant_value)
        timestamp = int(timestamp_value)
    except (TypeError, ValueError):
        return error_response(
            401,
            "authentication_failed",
            "销售之旅请求的认证信息格式无效。",
            "请由销售之旅系统重新生成签名后发送。",
        )
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
            body=await request.body(),
            supplied_signature=signature,
        )
    except InvalidSignature:
        return error_response(
            401,
            "authentication_failed",
            "销售之旅请求的签名无效或已经过期。",
            "请由销售之旅系统使用当前时间重新发送请求。",
        )
    return tenant_id


def build_materials_router(
    service: MaterialPackageReader,
    secret: Secret,
    *,
    now: Clock = lambda: int(time.time()),
) -> APIRouter:
    router = APIRouter(tags=["销售素材包"])

    @router.get(
        "/v1/products/{product_code}/materials/latest",
        summary="读取最新原始销售素材包",
        description="仅向已签名的销售之旅返回当前商家的原始图片素材和待处理状态。",
        response_model=None,
    )
    async def latest(product_code: str, request: Request) -> JSONResponse:
        resolved_secret = secret() if callable(secret) else secret
        tenant_id = await authenticated_sales_tenant(request, resolved_secret, now)
        if isinstance(tenant_id, JSONResponse):
            return tenant_id
        package = await service.latest(tenant_id, product_code)
        if package is None:
            return error_response(
                404,
                "material_package_not_found",
                "当前商家没有该商品的原始销售素材包。",
                "请先由相册整理提交包含商品编码的图片集。",
            )
        return JSONResponse(status_code=200, content=package.model_dump(mode="json"))

    return router


class ProductionMaterialPackageService:
    async def latest(
        self,
        tenant_id: UUID,
        product_code: str,
    ) -> ProductMaterialsUpdatedV1 | None:
        from product_engine.api.dependencies import get_runtime_database
        from product_engine.repositories.products import ProductRepository

        async with get_runtime_database().session() as session:
            return await MaterialPackageService(ProductRepository(session)).latest(
                tenant_id, product_code
            )
