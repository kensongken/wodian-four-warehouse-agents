"""Dedicated local demo API backed only by the physical demo sandbox."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from opc_style.api.dependencies import CustomerId
from opc_style.api.local_run_views import (
    project_local_run,
    read_candidate_response,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import DeterministicLocalImageGenerator
from opc_style.models import StyleSuiteRun
from opc_style.schemas import (
    LocalDemoBootstrapResponse,
    LocalDemoRunRequest,
    LocalDemoSlotDescriptor,
    LocalDemoStyleDescriptor,
    LocalStyleSuiteRunResponse,
    StyleSuiteExecutionResponse,
    StyleSuitePilotScope,
)
from opc_style.services.local_demo_sandbox import (
    LocalDemoSandbox,
    LocalDemoSandboxError,
)
from opc_style.services.real_photos import AssetContentStoreError
from opc_style.services.style_suites import StyleSuiteService
from opc_style.settings import StyleSuitePlanningSettings

router = APIRouter(tags=["local-demo"])
_CANDIDATE_URL_PREFIX = "/local/style-suite/api/demo/runs"


def _sandbox(request: Request) -> LocalDemoSandbox:
    sandbox = getattr(request.app.state, "local_demo_sandbox", None)
    if not isinstance(sandbox, LocalDemoSandbox):
        raise HTTPException(status_code=404)
    return sandbox


def bootstrap_demo_sandbox(
    request: Request, customer_id: UUID
) -> LocalDemoBootstrapResponse | JSONResponse:
    """Return fixed bootstrap facts from the sandbox-owned runtime only."""
    try:
        summary = _sandbox(request).bootstrap(customer_id)
    except LocalDemoSandboxError as error:
        return _sandbox_error_response(error)
    except OperationalError:
        return _sandbox_unavailable_response("local_demo_database_unavailable")
    except AssetContentStoreError:
        return _sandbox_unavailable_response("local_demo_storage_unavailable")
    return LocalDemoBootstrapResponse(
        sample_count=summary.sample_count,
        style_count=summary.style_count,
        slot_count=summary.slot_count,
        analysis_adapter="customer_confirmed_synthetic_history",
        styles=tuple(
            LocalDemoStyleDescriptor(
                name=style.name,
                slots=tuple(
                    LocalDemoSlotDescriptor(
                        slot_key=slot.slot_key,
                        label=slot.label,
                    )
                    for slot in style.slots
                ),
            )
            for style in summary.styles
        ),
    )


@router.post(
    "/local/style-suite/api/demo/bootstrap",
    response_model=LocalDemoBootstrapResponse,
    status_code=status.HTTP_201_CREATED,
)
def local_demo_bootstrap(
    request: Request, customer_id: CustomerId
) -> LocalDemoBootstrapResponse | JSONResponse:
    return bootstrap_demo_sandbox(request, customer_id)


@router.post(
    "/local/style-suite/api/demo/runs",
    response_model=StyleSuiteExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
def execute_local_demo_run(
    payload: LocalDemoRunRequest,
    request: Request,
    customer_id: CustomerId,
    response: Response,
) -> StyleSuiteExecutionResponse | JSONResponse:
    """Generate one deterministic three-candidate run inside the sandbox."""
    sandbox = _sandbox(request)
    try:
        sandbox.bootstrap(customer_id)
        product = sandbox.ensure_product(customer_id)
        canonical_scope = sandbox.canonical_scope(customer_id)
        subject_id = sandbox.subject_id(customer_id)
        with sandbox.session() as session:
            replay = session.scalar(
                select(StyleSuiteRun.id).where(
                    StyleSuiteRun.customer_id == subject_id,
                    StyleSuiteRun.idempotency_key == payload.idempotency_key,
                )
            )
            service = StyleSuiteService(
                session,
                settings=StyleSuitePlanningSettings(
                    real_generation_enabled=True,
                    candidate_estimate_usd=Decimal("0.12"),
                ),
                content_store=sandbox.store,
                generator=DeterministicLocalImageGenerator(),
                allow_synthetic_product_style=True,
                required_style_origin="local_demo",
            )
            result = service.create(
                subject_id,
                product.product_id,
                product.id,
                idempotency_key=payload.idempotency_key,
                pilot_scope=StyleSuitePilotScope(
                    scene_style_version_id=canonical_scope.scene_style_version_id,
                    slot_key=canonical_scope.slot_key,
                ),
            )
            if replay is not None:
                response.status_code = status.HTTP_200_OK
            return result
    except DomainRuleError as error:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": error.code, "retryable": False}},
        )
    except LocalDemoSandboxError as error:
        return _sandbox_error_response(error)
    except OperationalError:
        return _sandbox_unavailable_response("local_demo_database_unavailable")
    except AssetContentStoreError:
        return _sandbox_unavailable_response("local_demo_storage_unavailable")


@router.get(
    "/local/style-suite/api/demo/runs/{run_id}",
    response_model=LocalStyleSuiteRunResponse,
)
def local_demo_run_state(
    run_id: UUID,
    request: Request,
    customer_id: CustomerId,
) -> LocalStyleSuiteRunResponse | JSONResponse:
    sandbox = _sandbox(request)
    subject_id = sandbox.subject_id(customer_id)
    try:
        with sandbox.session() as session:
            return project_local_run(
                session,
                customer_id=subject_id,
                run_id=run_id,
                preview_secret=sandbox.preview_secret,
                candidate_url_prefix=_CANDIDATE_URL_PREFIX,
            )
    except DomainRuleError as error:
        raise HTTPException(status_code=404) from error
    except LocalDemoSandboxError as error:
        return _sandbox_error_response(error)
    except OperationalError:
        return _sandbox_unavailable_response("local_demo_database_unavailable")
    except AssetContentStoreError:
        return _sandbox_unavailable_response("local_demo_storage_unavailable")


@router.get(
    "/local/style-suite/api/demo/runs/{run_id}/candidates/{candidate_id}/content",
    response_class=Response,
)
def local_demo_candidate_content(
    run_id: UUID,
    candidate_id: UUID,
    request: Request,
    access: str = "",
    x_customer_id: Annotated[str | None, Header(alias="X-Customer-Id")] = None,
) -> Response:
    sandbox = _sandbox(request)
    header_customer_id: UUID | None = None
    if x_customer_id is not None:
        try:
            header_customer_id = sandbox.subject_id(UUID(x_customer_id))
        except ValueError as error:
            raise HTTPException(status_code=404) from error
    try:
        with sandbox.session() as session:
            return read_candidate_response(
                session,
                sandbox.store,
                secret=sandbox.preview_secret,
                run_id=run_id,
                candidate_id=candidate_id,
                access=access,
                header_customer_id=header_customer_id,
                propagate_store_error=True,
            )
    except LocalDemoSandboxError as error:
        return _sandbox_error_response(error)
    except OperationalError:
        return _sandbox_unavailable_response("local_demo_database_unavailable")
    except AssetContentStoreError:
        return _sandbox_unavailable_response("local_demo_media_unavailable")


def _sandbox_error_response(error: LocalDemoSandboxError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {"code": error.code, "retryable": error.retryable},
        },
    )


def _sandbox_unavailable_response(code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": {"code": code, "retryable": True}},
    )
