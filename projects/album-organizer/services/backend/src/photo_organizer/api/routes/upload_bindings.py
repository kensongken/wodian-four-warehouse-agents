from fastapi import APIRouter, Depends, HTTPException, Request, status

from photo_organizer.api.dependencies import require_opc_membership
from photo_organizer.catalog.upload_bindings import (
    UploadBindingConflict,
    UploadBindingInvalidCandidate,
    UploadBindingNotFound,
    UploadBindingService,
)
from photo_organizer.models.opc import OpcMembership
from photo_organizer.schemas.upload_bindings import (
    ControlledUploadBindingRequest,
    ControlledUploadBindingResponse,
    UploadBindingConflictError,
    UploadBindingInvalidCandidateError,
    UploadBindingNotFoundError,
    UploadBindingUnauthorizedError,
)

router = APIRouter(
    prefix="/v1/opcs/{opc_id}/upload-bindings",
    tags=["upload-bindings"],
)


def get_upload_binding_service(request: Request) -> UploadBindingService:
    return request.app.state.upload_binding_service


@router.post(
    "",
    response_model=ControlledUploadBindingResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": UploadBindingUnauthorizedError,
            "description": "Missing or invalid bearer token.",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": UploadBindingNotFoundError,
            "description": "OPC membership or task is unavailable.",
        },
        status.HTTP_409_CONFLICT: {
            "model": UploadBindingConflictError,
            "description": "Candidate or task cannot be safely bound.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": UploadBindingInvalidCandidateError,
            "description": "Candidate input is invalid or unsafe.",
        },
    },
)
async def bind_upload_candidate(
    opc_id: str,
    body: ControlledUploadBindingRequest,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: UploadBindingService = Depends(get_upload_binding_service),  # noqa: B008
) -> ControlledUploadBindingResponse:
    try:
        return await service.bind(
            opc_id=opc_id,
            user_id=membership.user_id,
            task_id=body.task_id,
            candidate=body.candidate,
        )
    except UploadBindingNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from None
    except UploadBindingInvalidCandidate:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid_candidate",
        ) from None
    except UploadBindingConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
