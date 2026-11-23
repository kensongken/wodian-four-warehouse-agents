"""Human-reviewed product-subject mask gate for identity-locked generation."""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.api.routes_image_jobs import (
    SubjectSegmenterDependency,
)
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.subject_seal import PRODUCT_SUBJECT_CANVAS, SubjectSealError
from opc_style.models import ProductSubjectSeal
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError
from opc_style.services.subject_seals import (
    ProductSubjectSealNotFoundError,
    ProductSubjectSealService,
    SubjectSealContentStore,
)

router = APIRouter(prefix="/v1/product-subjects", tags=["product-subjects"])
SessionDependency = Annotated[Session, Depends(get_session)]
ContentStoreDependency = Annotated[AssetContentStore, Depends(get_asset_content_store)]


class SubjectMaskPreviewRequest(BaseModel):
    """One customer-owned source asset selected for reusable mask review."""

    model_config = ConfigDict(extra="forbid")
    asset_id: UUID


def _response(
    row: ProductSubjectSeal, *, created: bool, reviewed: bool
) -> dict[str, object] | JSONResponse:
    return {
        "id": str(row.id),
        "product_asset_id": str(row.product_asset_id),
        "width": row.normalized_width,
        "height": row.normalized_height,
        "core_mask_pixel_count": row.core_mask_pixel_count,
        "adapter_version": row.segmentation_adapter_version,
        "review_state": "confirmed" if reviewed else "needs_human_review",
        "created": created,
        "publishable": False,
        "rgba_url": f"/v1/product-subjects/{row.id}/rgba",
        "mask_url": f"/v1/product-subjects/{row.id}/mask",
    }


@router.post("/preview", status_code=status.HTTP_201_CREATED, response_model=None)
def preview_subject_mask(
    request: SubjectMaskPreviewRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    segmenter: SubjectSegmenterDependency,
    response: Response,
) -> dict[str, object] | JSONResponse:
    """Extract once, persist privately, and return a human-reviewable subject."""
    try:
        result = ProductSubjectSealService(
            session, cast(SubjectSealContentStore, content_store)
        ).seal(
            customer_id,
            asset_id=request.asset_id,
            segmenter=segmenter,
            canvas_size=PRODUCT_SUBJECT_CANVAS,
        )
    except ProductSubjectSealNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except (DomainRuleError, SubjectSealError) as error:
        return _conflict(error.code)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _response(result.seal, created=result.created, reviewed=False)


@router.post(
    "/{seal_id}/confirm",
    status_code=status.HTTP_201_CREATED,
    response_model=None,
)
def confirm_subject_mask(
    seal_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    response: Response,
    mask_png: Annotated[bytes, Body(media_type="image/png")],
    confirmation_key: Annotated[str, Header(alias="X-Confirmation-Key")],
    confirmed: Annotated[str, Header(alias="X-Confirmed-Subject-Mask")],
) -> dict[str, object] | JSONResponse:
    """Create an immutable reviewed seal from an explicit erase-only mask."""
    if confirmed.lower() != "true":
        return _conflict("subject_mask_confirmation_required")
    if not mask_png or len(mask_png) > 8 * 1024 * 1024:
        return _conflict("subject_review_mask_invalid")
    try:
        result = ProductSubjectSealService(
            session, cast(SubjectSealContentStore, content_store)
        ).confirm_reviewed_mask(
            customer_id,
            base_seal_id=seal_id,
            reviewed_mask_png=mask_png,
            confirmation_key=confirmation_key,
        )
    except ProductSubjectSealNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except (DomainRuleError, SubjectSealError) as error:
        return _conflict(error.code)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _response(result.seal, created=result.created, reviewed=True)


@router.get("/{seal_id}/rgba")
def get_subject_rgba(
    seal_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
) -> Response:
    """Stream the private transparent subject for same-origin review."""
    try:
        row = ProductSubjectSealService(
            session, cast(SubjectSealContentStore, content_store)
        ).get(customer_id, seal_id)
        content = cast(SubjectSealContentStore, content_store).read_subject_artifact(
            customer_id, row.id, "rgba", row.normalized_subject_sha256
        )
    except (ProductSubjectSealNotFoundError, AssetContentStoreError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/{seal_id}/mask")
def get_subject_mask(
    seal_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
) -> Response:
    """Stream the immutable core mask for diagnostics and audit."""
    try:
        row = ProductSubjectSealService(
            session, cast(SubjectSealContentStore, content_store)
        ).get(customer_id, seal_id)
        content = cast(SubjectSealContentStore, content_store).read_subject_artifact(
            customer_id, row.id, "core-mask", row.mask_sha256
        )
    except (ProductSubjectSealNotFoundError, AssetContentStoreError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _conflict(code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": {"code": code, "retryable": False}},
    )
