from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.api.dependencies import require_opc_membership
from photo_organizer.catalog.manifest import ProductMaterialManifestService
from photo_organizer.models.opc import OpcMembership
from photo_organizer.schemas.product_material_manifest import (
    ProductMaterialManifestItem,
    ProductMaterialManifestResponse,
)

router = APIRouter(
    prefix="/v1/opcs/{opc_id}/product-material-manifest",
    tags=["product-material-manifest"],
)


def get_product_material_manifest_service(
    request: Request,
) -> ProductMaterialManifestService:
    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    return ProductMaterialManifestService(session_factory)


@router.get("", response_model=ProductMaterialManifestResponse)
async def get_product_material_manifest(
    opc_id: str,
    limit: int = Query(default=20, ge=1, le=50),
    before: str | None = None,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: ProductMaterialManifestService = Depends(  # noqa: B008
        get_product_material_manifest_service
    ),
) -> ProductMaterialManifestResponse:
    del membership
    cursor = _parse_cursor(before) if before is not None else None
    items = await service.list_items(opc_id=opc_id, limit=limit, before=cursor)
    return ProductMaterialManifestResponse(
        schema_version="opc-product-material-manifest/1.0",
        opc_id=opc_id,
        items=list(items),
        next_before=_cursor_for(items[-1]) if len(items) == limit else None,
    )


def _cursor_for(item: ProductMaterialManifestItem) -> str:
    timestamp = item.classification.confirmed_at.isoformat().replace("+00:00", "Z")
    return f"{timestamp},{item.classification.classification_id}"


def _parse_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        timestamp, classification_id = value.split(",", maxsplit=1)
        parsed_timestamp = datetime.fromisoformat(timestamp)
        if parsed_timestamp.tzinfo is None:
            raise ValueError("cursor timestamp requires timezone")
        return parsed_timestamp, UUID(classification_id)
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid_cursor",
        ) from error
