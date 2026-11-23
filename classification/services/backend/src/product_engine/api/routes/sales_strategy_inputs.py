import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from product_engine.api.dependencies import get_runtime_database
from product_engine.api.routes.album import error_response
from product_engine.api.routes.materials import authenticated_sales_tenant
from product_engine.contracts.sales_strategy import (
    SalesStrategyInputV1,
    canonical_sales_strategy_sha256,
)
from product_engine.repositories.sales_strategy_inputs import SalesStrategyInputRepository

Clock = Callable[[], int]
Secret = bytes | Callable[[], bytes]


class SalesStrategyInputReader(Protocol):
    async def latest(
        self,
        tenant_id: UUID,
        product_code: str,
    ) -> SalesStrategyInputV1 | None: ...

    async def by_version(
        self,
        tenant_id: UUID,
        product_code: str,
        version: int,
    ) -> SalesStrategyInputV1 | None: ...


def build_sales_strategy_inputs_router(
    service: SalesStrategyInputReader,
    secret: Secret,
    *,
    now: Clock = lambda: int(time.time()),
) -> APIRouter:
    router = APIRouter(tags=["销售策略输入"])

    @router.get(
        "/v0.1/sales-strategy-inputs/{product_code}",
        summary="读取销售策略安全输入",
        description="仅向已签名的销售之旅返回预览资料，不授权自动对客、发布、报价或发送微信。",
        response_model=None,
    )
    async def read_strategy_input(
        product_code: str,
        request: Request,
        version: int | None = Query(default=None, ge=1),
    ) -> JSONResponse:
        resolved_secret = secret() if callable(secret) else secret
        tenant_id = await authenticated_sales_tenant(request, resolved_secret, now)
        if isinstance(tenant_id, JSONResponse):
            return error_response(
                401,
                "invalid_sales_signature",
                "销售之旅请求的签名无效或已经过期。",
                "请由销售之旅使用当前商家身份重新发送请求。",
            )
        try:
            value = (
                await service.by_version(tenant_id, product_code, version)
                if version is not None
                else await service.latest(tenant_id, product_code)
            )
        except (ValidationError, ValueError):
            return error_response(
                409,
                "sales_strategy_input_conflict",
                "销售策略输入底账损坏或内容指纹冲突。",
                "请停止使用该资料并修复底账。",
            )
        if value is None:
            return error_response(
                404,
                "sales_strategy_input_not_found",
                "当前商家没有该商品的指定策略输入版本。",
                "请先由相册整理提交商品图组，或读取最新版本。",
            )
        if canonical_sales_strategy_sha256(value) != value.content_sha256:
            return error_response(
                409,
                "sales_strategy_input_conflict",
                "销售策略输入内容与指纹不一致。",
                "请停止使用该资料并修复底账。",
            )
        return JSONResponse(content=value.model_dump(mode="json", exclude_none=True))

    return router


class ProductionSalesStrategyInputService:
    async def latest(
        self,
        tenant_id: UUID,
        product_code: str,
    ) -> SalesStrategyInputV1 | None:
        async with get_runtime_database().session() as session:
            return await SalesStrategyInputRepository(session).latest(tenant_id, product_code)

    async def by_version(
        self,
        tenant_id: UUID,
        product_code: str,
        version: int,
    ) -> SalesStrategyInputV1 | None:
        async with get_runtime_database().session() as session:
            return await SalesStrategyInputRepository(session).by_version(
                tenant_id, product_code, version
            )
