from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from photo_organizer.api.dependencies import get_current_user
from photo_organizer.models.user import User
from photo_organizer.schemas.assets import (
    AssetInitRequest,
    AssetInitResponse,
    AssetResponse,
    CompleteAssetRequest,
    FailAssetRequest,
    SignPartRequest,
    SignPartResponse,
    UnsupportedAssetRequest,
)
from photo_organizer.services.upload_service import (
    InvalidParts,
    UploadConflict,
    UploadNotFound,
    UploadService,
)

router = APIRouter(tags=["assets"])


def get_upload_service(request: Request) -> UploadService:
    return request.app.state.upload_service


@router.post(
    "/v1/tasks/{task_id}/assets/initiate",
    response_model=AssetInitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def initiate_asset(
    task_id: UUID,
    body: AssetInitRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    service: UploadService = Depends(get_upload_service),  # noqa: B008
) -> AssetInitResponse:
    try:
        result = await service.initiate(user.id, task_id, body)
        return AssetInitResponse(
            asset_id=result.asset.id,
            already_uploaded=result.already_uploaded,
            provider_upload_id=result.provider_upload_id,
            completed_parts=result.completed_parts,
            disposition=result.disposition,
            canonical_asset_id=result.canonical_asset_id,
        )
    except (UploadNotFound, UploadConflict) as error:
        raise _upload_http_error(error) from error


@router.post("/v1/assets/{asset_id}/parts/sign", response_model=SignPartResponse)
async def sign_asset_part(
    asset_id: UUID,
    body: SignPartRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    service: UploadService = Depends(get_upload_service),  # noqa: B008
) -> SignPartResponse:
    try:
        result = await service.sign_part(user.id, asset_id, body.part_number)
        return SignPartResponse(url=result.url, expires_seconds=result.expires_seconds)
    except (UploadNotFound, UploadConflict) as error:
        raise _upload_http_error(error) from error


@router.post("/v1/assets/{asset_id}/complete", response_model=AssetResponse)
async def complete_asset(
    asset_id: UUID,
    body: CompleteAssetRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    service: UploadService = Depends(get_upload_service),  # noqa: B008
) -> AssetResponse:
    try:
        return AssetResponse.model_validate(
            (await service.complete(user.id, asset_id, body.parts)).asset
        )
    except (UploadNotFound, UploadConflict, InvalidParts) as error:
        raise _upload_http_error(error) from error


@router.post("/v1/assets/{asset_id}/fail", response_model=AssetResponse)
async def fail_asset(
    asset_id: UUID,
    body: FailAssetRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    service: UploadService = Depends(get_upload_service),  # noqa: B008
) -> AssetResponse:
    try:
        return AssetResponse.model_validate(
            await service.fail(user.id, asset_id, body.code, body.message)
        )
    except (UploadNotFound, UploadConflict) as error:
        raise _upload_http_error(error) from error


@router.post(
    "/v1/tasks/{task_id}/assets/unsupported",
    response_model=AssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def unsupported_asset(
    task_id: UUID,
    body: UnsupportedAssetRequest,
    user: User = Depends(get_current_user),  # noqa: B008
    service: UploadService = Depends(get_upload_service),  # noqa: B008
) -> AssetResponse:
    try:
        return AssetResponse.model_validate(
            await service.unsupported(user.id, task_id, body)
        )
    except (UploadNotFound, UploadConflict) as error:
        raise _upload_http_error(error) from error


def _upload_http_error(
    error: UploadNotFound | UploadConflict | InvalidParts,
) -> HTTPException:
    detail = str(error)
    if isinstance(error, UploadNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    if isinstance(error, InvalidParts):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail
        )
    if detail == "unsupported_media_type":
        return HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=detail
        )
    if detail == "image_too_large":
        return HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=detail
        )
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
