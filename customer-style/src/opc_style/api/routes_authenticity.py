"""Machine authenticity gate routes for customer-owned image jobs."""

from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.domain.authenticity import (
    SYNTHETIC_LOCAL_AUTHENTICITY_MODE,
    AuthenticityChecker,
    DeterministicLocalAuthenticityChecker,
)
from opc_style.schemas import (
    AuthenticityCandidateResponse,
    AuthenticityCheckResponse,
    AuthenticityGateResponse,
)
from opc_style.services.authenticity import (
    AuthenticityConflictError,
    AuthenticityGateOutput,
    AuthenticityJobNotFoundError,
    AuthenticityOutcomeUncertainError,
    AuthenticityService,
)
from opc_style.services.real_photos import AssetContentStore

router = APIRouter(prefix="/v1/image-outputs", tags=["image-outputs"])
SessionDependency = Annotated[Session, Depends(get_session)]
ContentStoreDependency = Annotated[AssetContentStore, Depends(get_asset_content_store)]


def get_authenticity_checker() -> AuthenticityChecker:
    """Return the fixed local rule checker; no visual-AI service is configured."""
    return DeterministicLocalAuthenticityChecker()


CheckerDependency = Annotated[AuthenticityChecker, Depends(get_authenticity_checker)]


@router.post(
    "/{job_id}/authenticity-checks",
    response_model=AuthenticityGateResponse,
    status_code=status.HTTP_201_CREATED,
)
def run_authenticity_checks(
    job_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    checker: CheckerDependency,
    response: Response,
) -> AuthenticityGateResponse | JSONResponse:
    """Run or safely replay the mandatory machine check for exactly three images."""
    try:
        result = AuthenticityService(session, content_store, checker).run(
            customer_id, job_id
        )
    except AuthenticityJobNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except AuthenticityConflictError as error:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": error.code, "retryable": False}},
        )
    except AuthenticityOutcomeUncertainError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "authenticity_artifact_outcome_uncertain",
                    "retryable": True,
                }
            },
        )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return authenticity_gate_response(result)


def authenticity_gate_response(
    result: AuthenticityGateOutput,
) -> AuthenticityGateResponse:
    """Hide artifact paths and state explicitly that no human action occurred."""
    return AuthenticityGateResponse(
        id=result.job.id,
        status=result.job.status,
        failure_reason=result.job.failure_reason,
        checker_version=result.checker_version,
        check_mode=cast(
            Literal["synthetic_local_rule_based"], SYNTHETIC_LOCAL_AUTHENTICITY_MODE
        ),
        visual_ai=False,
        customer_approved=False,
        publishable=False,
        candidates=[
            AuthenticityCandidateResponse(
                id=item.candidate.id,
                sequence_number=item.candidate.sequence_number,
                candidate_state=item.state,
                checks=[
                    AuthenticityCheckResponse(
                        check_type=check.check_type,
                        attempt=check.attempt,
                        passed=check.passed,
                        reason=check.reason,
                        checker_version=check.checker_version,
                        details=check.details,
                        created_at=_as_utc(check.created_at),
                    )
                    for check in item.checks
                ],
            )
            for item in result.candidates
        ],
        updated_at=_as_utc(result.job.updated_at),
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
