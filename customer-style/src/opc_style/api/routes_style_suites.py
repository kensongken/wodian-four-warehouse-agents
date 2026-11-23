"""Customer-isolated previews for a complete historical style suite."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.api.routes_image_jobs import GeneratorDependency
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.domain.errors import DomainRuleError
from opc_style.schemas import (
    StyleSuiteCreateRequest,
    StyleSuiteExecutionResponse,
    StyleSuitePreviewRequest,
    StyleSuitePreviewResponse,
)
from opc_style.services.real_photos import AssetContentStore
from opc_style.services.style_suites import StyleSuiteService

router = APIRouter(prefix="/v1/style-suites", tags=["style-suites"])
SessionDependency = Annotated[Session, Depends(get_session)]
ContentStoreDependency = Annotated[AssetContentStore, Depends(get_asset_content_store)]


@router.post("/preview", response_model=StyleSuitePreviewResponse)
def preview_style_suite(
    request: StyleSuitePreviewRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    http_request: Request,
) -> StyleSuitePreviewResponse | JSONResponse:
    try:
        return StyleSuiteService(
            session,
            settings=http_request.app.state.style_suite_planning_settings,
            enforce_real_pilot_boundary=True,
            required_style_origin="customer_upload",
        ).preview(customer_id, request.product_id, request.source_asset_id)
    except DomainRuleError as error:
        return JSONResponse(
            status_code=409,
            content={
                "error": {"code": error.code, "retryable": False},
            },
        )


@router.post(
    "",
    response_model=StyleSuiteExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_style_suite(
    request: StyleSuiteCreateRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    generator: GeneratorDependency,
    response: Response,
    http_request: Request,
) -> StyleSuiteExecutionResponse | JSONResponse:
    try:
        result = StyleSuiteService(
            session,
            settings=http_request.app.state.style_suite_planning_settings,
            content_store=content_store,
            generator=generator,
            enforce_real_pilot_boundary=True,
        ).create(
            customer_id,
            request.product_id,
            request.source_asset_id,
            idempotency_key=request.idempotency_key,
        )
        if result.state == "planned":
            response.status_code = status.HTTP_200_OK
        return result
    except DomainRuleError as error:
        code = 404 if error.code == "style_suite_run_not_found" else 409
        return JSONResponse(
            status_code=code,
            content={"error": {"code": error.code, "retryable": False}},
        )


@router.get("/{run_id}", response_model=StyleSuiteExecutionResponse)
def get_style_suite(
    run_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
) -> StyleSuiteExecutionResponse | JSONResponse:
    try:
        return StyleSuiteService(session).get(customer_id, run_id)
    except DomainRuleError as error:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": error.code, "retryable": False}},
        )
