from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from product_engine.presentation.zh_cn import user_message

ReadinessProbe = Callable[[], Awaitable[bool]]
ERROR_CODE = "dependencies_unavailable"


def build_health_router(probe: ReadinessProbe) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get(
        "/health/live",
        summary="检查服务进程",
        description="确认服务进程正常；该检查不访问数据库或图片存储。",
    )
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get(
        "/health/ready",
        summary="检查服务是否可接收请求",
        description="确认数据库、图片存储等依赖服务可用，服务可以接收业务请求。",
        response_model=None,
    )
    async def ready() -> dict[str, str] | JSONResponse:
        if not await probe():
            message = user_message(ERROR_CODE)
            return JSONResponse(
                status_code=503,
                content={
                    "error_code": ERROR_CODE,
                    "message_zh": message.message_zh,
                    "next_step_zh": message.next_step_zh,
                },
            )
        return {"status": "ready"}

    return router
