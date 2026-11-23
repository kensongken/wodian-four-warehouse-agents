"""Identity-only API routes for customer-owned real-photo normalization."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.domain.errors import DomainRuleError
from opc_style.schemas import (
    RealPhotoCandidateResponse,
    RealPhotoOutputRequest,
    RealPhotoOutputResponse,
)
from opc_style.services.real_photos import (
    AssetContentStore,
    RealPhotoOutput,
    RealPhotoOutputNotFoundError,
    RealPhotoOutputOutcomeUncertainError,
    RealPhotoOutputService,
    UnconfiguredAssetContentStore,
)

router = APIRouter(prefix="/v1/image-outputs", tags=["image-outputs"])
SessionDependency = Annotated[Session, Depends(get_session)]


def get_asset_content_store(request: Request) -> AssetContentStore:
    """Resolve only an explicitly app-configured store, otherwise fail closed."""
    configured = getattr(request.app.state, "classification_bridge_store", None)
    if configured is None:
        return UnconfiguredAssetContentStore()
    return cast(AssetContentStore, configured)


ContentStoreDependency = Annotated[AssetContentStore, Depends(get_asset_content_store)]


@router.post(
    "/real-photo",
    response_model=RealPhotoOutputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_real_photo_output(
    request: RealPhotoOutputRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    response: Response,
) -> RealPhotoOutputResponse | JSONResponse:
    """Normalize a trusted internal asset selected only by customer-owned identity."""
    try:
        result = RealPhotoOutputService(session, content_store).create(
            customer_id,
            product_id=request.product_id,
            asset_id=request.asset_id,
            profile=request.profile,
            idempotency_key=request.idempotency_key,
        )
    except RealPhotoOutputNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except RealPhotoOutputOutcomeUncertainError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "image_generation_outcome_uncertain",
                    "retryable": True,
                }
            },
        )
    except DomainRuleError as error:
        if error.code not in {
            "image_output_idempotency_conflict",
            "organizer_confirmation_missing",
            "transform_permission_unknown",
            "transform_permission_denied",
        }:
            raise
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=jsonable_encoder(
                {"error": {"code": error.code, "retryable": False}}
            ),
        )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return real_photo_output_response(result)


def real_photo_output_response(
    result: RealPhotoOutput,
    *,
    candidate_states: Mapping[UUID, str] | None = None,
) -> RealPhotoOutputResponse:
    job = result.job
    metadata = job.request_metadata
    return RealPhotoOutputResponse(
        id=job.id,
        product_id=str(metadata["product_id"]),
        asset_id=UUID(str(metadata["asset_id"])),
        output_type=cast(Literal["real_photo"], job.output_type),
        profile=str(metadata["profile"]),
        adapter_version=job.adapter_version,
        status=job.status,
        failure_reason=job.failure_reason,
        publishable=False,
        candidates=[
            RealPhotoCandidateResponse(
                id=candidate.id,
                sequence_number=candidate.sequence_number,
                identity=candidate.identity,
                candidate_state=(
                    candidate_states.get(candidate.id, "pending_authenticity_check")
                    if candidate_states is not None
                    else "pending_authenticity_check"
                ),
                content_sha256=candidate.content_sha256,
                mime_type=candidate.mime_type,
                width=candidate.width,
                height=candidate.height,
                adapter_version=candidate.adapter_version,
                metadata=candidate.artifact_metadata,
            )
            for candidate in result.candidates
        ],
        created_at=_as_utc(job.created_at),
        updated_at=_as_utc(job.updated_at),
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
