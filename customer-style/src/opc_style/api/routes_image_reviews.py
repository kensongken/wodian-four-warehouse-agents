"""Customer-owned image review and dual-view product routes."""

from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.models import ProductAsset
from opc_style.schemas import (
    ApprovedImageReferenceResponse,
    ImageReviewRequest,
    ImageReviewResponse,
    ProductDualImageAssociationResponse,
    ProductImageViewResponse,
    RealPhotoImageReferenceResponse,
    StyleImageProductViewResponse,
)
from opc_style.services.image_reviews import (
    ImageReviewConflictError,
    ImageReviewNotFoundError,
    ImageReviewOutcomeUncertainError,
    ImageReviewOutput,
    ImageReviewService,
    ProductImageViewOutput,
)
from opc_style.services.real_photos import AssetContentStore

router = APIRouter(tags=["image-outputs"])
SessionDependency = Annotated[Session, Depends(get_session)]
ContentStoreDependency = Annotated[AssetContentStore, Depends(get_asset_content_store)]


@router.post(
    "/v1/image-outputs/{job_id}/candidates/{candidate_id}/review",
    response_model=ImageReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def review_image_candidate(
    job_id: UUID,
    candidate_id: UUID,
    request: ImageReviewRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    response: Response,
) -> ImageReviewResponse | JSONResponse:
    """Append or replay one customer decision after exact artifact re-attestation."""
    try:
        result = ImageReviewService(session, content_store).decide(
            customer_id,
            job_id,
            candidate_id,
            decision=request.decision,
            reason=request.reason,
            idempotency_key=request.idempotency_key,
            fidelity_state=request.fidelity_state,
            fidelity_flags=request.fidelity_flags,
            fidelity_warning_acknowledged=(
                request.fidelity_warning_acknowledged
            ),
        )
    except ImageReviewNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except ImageReviewConflictError as error:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": error.code, "retryable": False}},
        )
    except ImageReviewOutcomeUncertainError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "review_artifact_outcome_uncertain",
                    "retryable": True,
                }
            },
        )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return image_review_response(result)


@router.get(
    "/v1/products/{product_id}/images",
    response_model=ProductImageViewResponse,
)
def get_product_images(
    product_id: str,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    view: Literal["default", "real"] = "default",
) -> ProductImageViewResponse | JSONResponse:
    """Return approved style by default or immutable real photos on request."""
    try:
        result = ImageReviewService(session, content_store).get_product_images(
            customer_id, product_id, view=view
        )
    except ImageReviewNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except ImageReviewOutcomeUncertainError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "review_artifact_outcome_uncertain",
                    "retryable": True,
                }
            },
        )
    return product_image_view_response(result)


def image_review_response(result: ImageReviewOutput) -> ImageReviewResponse:
    """Expose review and dual-image identities without any storage path or publish API."""
    candidate = result.candidate
    source_product_ids = [asset.product_id for asset in result.source_assets]
    source_asset_ids = [asset.id for asset in result.source_assets]
    approved_reference = ApprovedImageReferenceResponse(
        image_kind=result.image_kind,
        candidate_id=candidate.id,
        identity=candidate.identity,
        content_sha256=candidate.content_sha256,
        mime_type=candidate.mime_type,
        width=candidate.width,
        height=candidate.height,
        customer_approved=True,
        publishable=False,
        source_product_ids=source_product_ids,
        source_asset_ids=source_asset_ids,
    )
    approved = result.review.decision == "approved"
    return ImageReviewResponse(
        review_id=result.review.id,
        job_id=result.job.id,
        candidate_id=candidate.id,
        decision=cast(
            Literal[
                "approved",
                "not_my_style",
                "product_detail_inaccurate",
                "rejected",
            ],
            result.review.decision,
        ),
        reason=result.review.reason,
        job_status=cast(
            Literal[
                "awaiting_customer_review", "customer_approved", "customer_rejected"
            ],
            result.job.status,
        ),
        candidate_state="customer_approved" if approved else "customer_rejected",
        image_kind=result.image_kind,
        customer_approved=approved,
        publishable=False,
        source_product_ids=source_product_ids,
        source_asset_ids=source_asset_ids,
        product_associations=[
            ProductDualImageAssociationResponse(
                product_id=item.association.product_id,
                approved_image=approved_reference,
                real_photo=_real_photo_reference(item.source_asset),
            )
            for item in result.associations
        ],
        created_at=_as_utc(result.review.created_at),
        suite_state=result.suite_state,
    )


def product_image_view_response(
    result: ProductImageViewOutput,
) -> ProductImageViewResponse:
    """Serialize the resolved backend display contract with truthful identities."""
    images: list[RealPhotoImageReferenceResponse | StyleImageProductViewResponse]
    if result.style_candidate is None:
        images = [_real_photo_reference(asset) for asset in result.real_assets]
    else:
        candidate = result.style_candidate
        images = [
            StyleImageProductViewResponse(
                image_kind="style_image",
                candidate_id=candidate.id,
                identity=candidate.identity,
                content_sha256=candidate.content_sha256,
                mime_type=candidate.mime_type,
                width=candidate.width,
                height=candidate.height,
                customer_approved=True,
                publishable=False,
                source_product_ids=_string_list(
                    candidate.artifact_metadata.get("source_product_ids")
                ),
                source_asset_ids=[
                    UUID(value)
                    for value in _string_list(
                        candidate.artifact_metadata.get("source_asset_ids")
                    )
                ],
            )
        ]
    return ProductImageViewResponse(
        product_id=result.product_id,
        requested_view=result.requested_view,
        resolved_view=result.resolved_view,
        images=images,
    )


def _real_photo_reference(asset: ProductAsset) -> RealPhotoImageReferenceResponse:
    return RealPhotoImageReferenceResponse(
        image_kind="real_photo",
        asset_id=asset.id,
        identity=asset.identity,
        content_sha256=asset.content_sha256,
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return cast(list[str], value)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
