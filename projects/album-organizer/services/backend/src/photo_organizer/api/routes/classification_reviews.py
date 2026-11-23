from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.api.dependencies import require_opc_membership
from photo_organizer.categorization.review import (
    ClassificationReviewConflict,
    ClassificationReviewNotFound,
    ClassificationReviewRecord,
    ClassificationReviewService,
)
from photo_organizer.models.opc import OpcMembership
from photo_organizer.schemas.classification_reviews import (
    ClassificationReviewListResponse,
    ClassificationReviewResponse,
    ConfirmClassificationRequest,
)

router = APIRouter(
    prefix="/v1/opcs/{opc_id}/classification-reviews",
    tags=["classification-reviews"],
)


def get_classification_review_service(request: Request) -> ClassificationReviewService:
    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    return ClassificationReviewService(session_factory)


@router.get("", response_model=ClassificationReviewListResponse)
async def list_classification_reviews(
    opc_id: str,
    review_status: Literal["open", "resolved"] = Query(default="open", alias="status"),
    limit: int = Query(default=20, ge=1, le=50),
    before: str | None = None,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: ClassificationReviewService = Depends(  # noqa: B008
        get_classification_review_service
    ),
) -> ClassificationReviewListResponse:
    del membership
    cursor = _parse_cursor(before) if before is not None else None
    records = await service.list_reviews(
        opc_id=opc_id,
        review_status=review_status,
        limit=limit,
        before=cursor,
    )
    return ClassificationReviewListResponse(
        items=[_response(record) for record in records],
        next_before=_cursor_for(records[-1]) if len(records) == limit else None,
    )


@router.get("/{review_id}", response_model=ClassificationReviewResponse)
async def get_classification_review(
    opc_id: str,
    review_id: UUID,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: ClassificationReviewService = Depends(  # noqa: B008
        get_classification_review_service
    ),
) -> ClassificationReviewResponse:
    del membership
    try:
        return _response(await service.get_review(opc_id=opc_id, review_id=review_id))
    except ClassificationReviewNotFound as error:
        raise _not_found(error) from error


@router.post(
    "/{review_id}/confirm",
    response_model=ClassificationReviewResponse,
)
async def confirm_classification_review(
    opc_id: str,
    review_id: UUID,
    body: ConfirmClassificationRequest,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: ClassificationReviewService = Depends(  # noqa: B008
        get_classification_review_service
    ),
) -> ClassificationReviewResponse:
    try:
        await service.confirm(
            opc_id=opc_id,
            review_id=review_id,
            level_1=body.level_1.value,
            level_2=body.level_2.value,
            confirmed_by=f"user:{membership.user_id}",
        )
        return _response(await service.get_review(opc_id=opc_id, review_id=review_id))
    except ClassificationReviewNotFound as error:
        raise _not_found(error) from error
    except ClassificationReviewConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


def _response(record: ClassificationReviewRecord) -> ClassificationReviewResponse:
    classification = record.classification
    review = record.review
    return ClassificationReviewResponse.model_validate(
        {
            "id": review.id,
            "opc_id": review.opc_id,
            "classification_id": classification.id,
            "album_product_id": classification.album_product_id,
            "source_type": classification.source_type,
            "input_snapshot_version": classification.input_snapshot_version,
            "classification_version": classification.classification_version,
            "candidates": classification.candidates,
            "status": review.status,
            "confirmed_level_1": classification.confirmed_level_1,
            "confirmed_level_2": classification.confirmed_level_2,
            "confirmed_by": classification.confirmed_by,
            "decided_at": review.decided_at,
            "created_at": review.created_at,
            "updated_at": review.updated_at,
            "claims_not_evaluated": (
                "authenticity",
                "origin",
                "quality",
                "warehouse_acceptance",
            ),
            "mall_product_id": None,
            "bead_id": None,
        }
    )


def _cursor_for(record: ClassificationReviewRecord) -> str:
    return (
        f"{record.review.created_at.isoformat().replace('+00:00', 'Z')},"
        f"{record.review.id}"
    )


def _parse_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        timestamp, review_id = value.split(",", maxsplit=1)
        parsed_timestamp = datetime.fromisoformat(timestamp)
        if parsed_timestamp.tzinfo is None:
            raise ValueError
        return parsed_timestamp, UUID(review_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid_cursor",
        ) from error


def _not_found(error: ClassificationReviewNotFound) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=str(error),
    )
