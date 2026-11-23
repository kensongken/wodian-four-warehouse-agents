"""Customer-isolated API routes for style sample consent."""

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.domain.errors import DomainRuleError
from opc_style.models import StyleSample
from opc_style.repositories import StyleRepository
from opc_style.schemas import (
    SampleReadinessResponse,
    StyleSampleAuthorizeRequest,
    StyleSampleContentHeaders,
    StyleSampleContentResponse,
    StyleSampleResponse,
)
from opc_style.services.real_photos import (
    AssetContentStore,
    AssetContentStoreError,
    UnconfiguredAssetContentStore,
)
from opc_style.services.samples import StyleSampleNotFoundError, StyleSampleService

router = APIRouter(prefix="/v1/style-samples", tags=["style-samples"])
SessionDependency = Annotated[Session, Depends(get_session)]


def _content_store(request: Request) -> AssetContentStore:
    """Use an explicitly configured private store; raw bytes never become paths."""
    configured = getattr(request.app.state, "classification_bridge_store", None)
    if configured is None:
        return UnconfiguredAssetContentStore()
    return cast(AssetContentStore, configured)


@router.post(
    "/content",
    response_model=StyleSampleContentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def authorize_style_sample_content(
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    response: Response,
    x_photo_id: Annotated[str, Header(alias="X-Photo-Id")],
    x_content_sha256: Annotated[str, Header(alias="X-Content-Sha256")],
    x_mime_type: Annotated[str, Header(alias="X-Mime-Type")],
    x_consent_granted: Annotated[str, Header(alias="X-Consent-Granted")],
    x_consent_provenance: Annotated[str, Header(alias="X-Consent-Provenance")],
    x_consent_text_version: Annotated[str, Header(alias="X-Consent-Text-Version")],
) -> StyleSampleContentResponse | JSONResponse:
    """Seal authorized raw JPEG/PNG bytes under the caller's customer identity."""
    try:
        metadata = StyleSampleContentHeaders.model_validate(
            {
                "photo_id": x_photo_id,
                "claimed_sha256": x_content_sha256,
                "mime_type": x_mime_type,
                "consent_granted": x_consent_granted,
                "consent_provenance": x_consent_provenance,
                "consent_text_version": x_consent_text_version,
            }
        )
    except ValidationError:
        return _content_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "style_sample_content_metadata_invalid",
        )
    try:
        existing = StyleRepository(session).get_sample_by_photo(
            customer_id, metadata.photo_id
        )
        selection = StyleSampleService(session, _content_store(request)).authorize_content(
            customer_id,
            photo_id=metadata.photo_id,
            content=await request.body(),
            claimed_sha256=metadata.claimed_sha256,
            mime_type=metadata.mime_type,
            consent_provenance=metadata.consent_provenance,
            consent_text_version=metadata.consent_text_version,
        )
    except DomainRuleError as error:
        return _content_error(status.HTTP_409_CONFLICT, error.code)
    except AssetContentStoreError:
        return _content_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "style_sample_content_storage_unavailable",
            retryable=True,
        )
    sample = session.get(StyleSample, selection.sample_id)
    assert sample is not None
    if existing is not None:
        response.status_code = status.HTTP_200_OK
    return _content_response(sample)


@router.post("", response_model=StyleSampleResponse, status_code=status.HTTP_201_CREATED)
def authorize_style_sample(
    request: StyleSampleAuthorizeRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    response: Response,
) -> StyleSampleResponse | JSONResponse:
    """Store a customer's explicit consent before a photo becomes usable."""
    try:
        authorization = StyleSampleService(session).authorize(
            customer_id,
            photo_id=request.photo_id,
            consent_text_version=request.consent_text_version,
        )
    except DomainRuleError as error:
        if error.code not in {
            "style_sample_consent_version_conflict",
            "style_sample_consent_withdrawn",
        }:
            raise
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=jsonable_encoder(
                {"error": {"code": error.code, "retryable": False}}
            ),
        )
    if not authorization.created:
        response.status_code = status.HTTP_200_OK
    return _sample_response(authorization.sample)


@router.get("/readiness", response_model=SampleReadinessResponse)
def get_style_sample_readiness(
    customer_id: CustomerId, session: SessionDependency
) -> SampleReadinessResponse:
    """Show how many active, authorized samples remain for this customer."""
    readiness = StyleSampleService(session).readiness(customer_id)
    return SampleReadinessResponse(**readiness.__dict__)


@router.post("/{sample_id}/withdraw", response_model=StyleSampleResponse)
def withdraw_style_sample(
    sample_id: UUID, customer_id: CustomerId, session: SessionDependency
) -> StyleSampleResponse:
    """Revoke one customer's consent while retaining durable audit evidence."""
    try:
        sample = StyleSampleService(session).withdraw(customer_id, sample_id)
    except StyleSampleNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return _sample_response(sample)


def _sample_response(sample: StyleSample) -> StyleSampleResponse:
    """Translate the mutable persistence record into its safe HTTP state."""
    return StyleSampleResponse(
        id=sample.id,
        photo_id=sample.photo_id,
        status="withdrawn" if sample.withdrawn_at is not None else "active",
        authorized_at=sample.authorized_at,
        withdrawn_at=sample.withdrawn_at,
    )


def _content_response(sample: StyleSample) -> StyleSampleContentResponse:
    """Return public identifiers and decoded facts, never store internals."""
    assert sample.content_sha256 is not None
    assert sample.mime_type in {"image/jpeg", "image/png"}
    assert sample.width is not None
    assert sample.height is not None
    return StyleSampleContentResponse(
        **_sample_response(sample).model_dump(),
        content_sha256=sample.content_sha256,
        mime_type=cast(Literal["image/jpeg", "image/png"], sample.mime_type),
        width=sample.width,
        height=sample.height,
    )


def _content_error(
    status_code: int, code: str, *, retryable: bool = False
) -> JSONResponse:
    """Return a stable failure without leaking validation or storage details."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "retryable": retryable}},
    )
