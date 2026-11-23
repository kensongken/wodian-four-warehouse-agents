from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Protocol, cast

import boto3  # type: ignore[import-untyped]
from fastapi import FastAPI

from product_engine.api.dependencies import (
    DependencyProbe,
    get_runtime_database,
    get_runtime_settings,
)
from product_engine.api.routes.album import album_router
from product_engine.api.routes.health import build_health_router
from product_engine.api.routes.materials import (
    ProductionMaterialPackageService,
    build_materials_router,
)
from product_engine.api.routes.sales import ProductionSalesUsageService, build_sales_router
from product_engine.api.routes.sales_strategy_inputs import (
    ProductionSalesStrategyInputService,
    build_sales_strategy_inputs_router,
)
from product_engine.api.routes.screenshots import (
    ProductionScreenshotService,
    build_screenshots_router,
)
from product_engine.api.routes.templates import templates_router
from product_engine.presentation.zh_cn import (
    APP_DESCRIPTION,
    APP_TITLE,
    HEALTH_TAG_DESCRIPTION,
)

ReadinessProbe = Callable[[], Awaitable[bool]]


class S3Client(Protocol):
    def head_bucket(self, *, Bucket: str) -> dict[str, Any]: ...


async def default_probe() -> bool:
    return True


@lru_cache
def build_production_probe() -> DependencyProbe:
    settings = get_runtime_settings()
    database = get_runtime_database()
    client = cast(
        S3Client,
        boto3.client(
            "s3",
            endpoint_url=str(settings.s3_endpoint_url),
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        ),
    )

    def check_bucket() -> None:
        client.head_bucket(Bucket=settings.s3_bucket)

    return DependencyProbe(database.ping, check_bucket)


async def production_probe() -> bool:
    return await build_production_probe()()


def create_app(readiness_probe: ReadinessProbe | None = None) -> FastAPI:
    app = FastAPI(
        title=APP_TITLE,
        description=APP_DESCRIPTION,
        version="0.1.0",
        openapi_tags=[{"name": "health", "description": HEALTH_TAG_DESCRIPTION}],
    )

    @app.get(
        "/",
        summary="查看产品入口",
        description="返回产品中文名称、服务说明和接口文档地址。",
    )
    async def root() -> dict[str, str]:
        return {
            "product_name": APP_TITLE,
            "description": APP_DESCRIPTION,
            "docs_url": "/docs",
        }

    app.include_router(build_health_router(readiness_probe or default_probe))
    app.include_router(album_router)
    app.include_router(templates_router)
    app.include_router(
        build_materials_router(
            ProductionMaterialPackageService(),
            lambda: get_runtime_settings().sales_signing_secret.get_secret_value().encode(),
        )
    )
    app.include_router(
        build_sales_router(
            ProductionSalesUsageService(),
            lambda: get_runtime_settings().sales_signing_secret.get_secret_value().encode(),
        )
    )
    app.include_router(
        build_sales_strategy_inputs_router(
            ProductionSalesStrategyInputService(),
            lambda: get_runtime_settings().sales_signing_secret.get_secret_value().encode(),
        )
    )
    app.include_router(
        build_screenshots_router(
            ProductionScreenshotService(),
            lambda: get_runtime_settings().sales_signing_secret.get_secret_value().encode(),
        )
    )
    return app


app = create_app(readiness_probe=production_probe)
