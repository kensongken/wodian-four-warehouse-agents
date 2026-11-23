from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from photo_organizer.api.dependencies import require_opc_membership
from photo_organizer.catalog.downloads import (
    MaterialNotFound,
    MaterialTemporarilyUnavailable,
    MaterialUnavailable,
    ProductMaterialDownloadService,
)
from photo_organizer.models.opc import OpcMembership
from photo_organizer.schemas.product_material_download import (
    ProductMaterialDownloadResponse,
)

router = APIRouter(
    prefix="/v1/opcs/{opc_id}/product-material-manifest",
    tags=["product-material-manifest"],
)


def get_product_material_download_service(
    request: Request,
) -> ProductMaterialDownloadService:
    return request.app.state.product_material_download_service


@router.post(
    "/{confirmation_event_id}/assets/{asset_id}/download",
    response_model=ProductMaterialDownloadResponse,
)
async def sign_product_material_download(
    opc_id: str,
    confirmation_event_id: UUID,
    asset_id: UUID,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: ProductMaterialDownloadService = Depends(  # noqa: B008
        get_product_material_download_service
    ),
) -> ProductMaterialDownloadResponse:
    del membership
    try:
        result = await service.sign(
            opc_id=opc_id,
            confirmation_event_id=confirmation_event_id,
            asset_id=asset_id,
        )
    except MaterialNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from None
    except MaterialTemporarilyUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from None
    except MaterialUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from None
    return ProductMaterialDownloadResponse(
        schema_version="opc-product-material-download/1.0",
        confirmation_event_id=confirmation_event_id,
        asset_id=asset_id,
        download_url=result.download_url,
        expires_seconds=300,
    )
