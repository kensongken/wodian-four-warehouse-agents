"""Customer-isolated routes for confirmed product-asset metadata."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.domain.errors import DomainRuleError
from opc_style.schemas import (
    ProductAssetCollectionResponse,
    ProductAssetIntakeRequest,
    ProductAssetResponse,
)
from opc_style.services.assets import (
    ProductAssetIntake,
    ProductAssetService,
    ProductAssetsNotFoundError,
)

router = APIRouter(prefix="/v1/product-assets", tags=["product-assets"])
SessionDependency = Annotated[Session, Depends(get_session)]


@router.post("", response_model=ProductAssetResponse, status_code=status.HTTP_201_CREATED)
def intake_product_asset(
    request: ProductAssetIntakeRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    response: Response,
) -> ProductAssetResponse | JSONResponse:
    """Accept confirmed metadata without accepting media or server file paths."""
    try:
        result = ProductAssetService(session).intake(customer_id, request)
    except DomainRuleError as error:
        if error.code not in {
            "product_asset_content_conflict",
            "product_asset_facts_conflict",
            "product_asset_permission_conflict",
            "product_asset_permission_history_missing",
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
    return _asset_response(result)


@router.get("/{product_id}", response_model=ProductAssetCollectionResponse)
def get_product_assets(
    product_id: str, customer_id: CustomerId, session: SessionDependency
) -> ProductAssetCollectionResponse:
    """List a product's source assets without revealing another customer exists."""
    try:
        assets = ProductAssetService(session).get_product_assets(
            customer_id, product_id
        )
    except ProductAssetsNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return ProductAssetCollectionResponse(
        product_id=product_id,
        assets=[_asset_response(asset) for asset in assets],
    )


def _asset_response(result: ProductAssetIntake) -> ProductAssetResponse:
    asset = result.asset
    readiness = result.readiness
    return ProductAssetResponse(
        id=asset.id,
        product_id=asset.product_id,
        photo_id=asset.photo_id,
        identity=asset.identity,
        confirmed=asset.confirmed,
        source_type=asset.source_type,
        quality_flags=asset.quality_flags,
        occurred_at=_as_utc(asset.occurred_at),
        transform_permission=readiness.transform_permission,
        content_sha256=asset.content_sha256,
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
        generation_ready=readiness.generation_ready,
        readiness_reasons=list(readiness.reasons),
        created_at=_as_utc(asset.created_at),
    )


def _as_utc(value: datetime) -> datetime:
    """Return canonical UTC timestamps even when SQLite returned a naive value."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
