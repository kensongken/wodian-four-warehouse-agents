from fastapi import APIRouter, Depends, HTTPException, Request, status

from photo_organizer.api.dependencies import require_opc_membership
from photo_organizer.catalog.group_confirmation import (
    AlbumProductGroupConfirmationService,
    EvidenceGroupConflict,
    EvidenceGroupNotFound,
    ExistingProductConfirmationConflict,
    ExistingProductNotFound,
    OpcOwnerRequired,
    SnapshotVersionConflict,
)
from photo_organizer.models.opc import OpcMembership
from photo_organizer.schemas.group_confirmation import (
    AlbumProductGroupConfirmationRequest,
    AlbumProductGroupConfirmationResponse,
    ExistingProductNextSnapshotRequest,
    ExistingProductNextSnapshotResponse,
)

router = APIRouter(
    prefix="/v1/opcs/{opc_id}/album-products",
    tags=["album-products"],
)


def get_group_confirmation_service(
    request: Request,
) -> AlbumProductGroupConfirmationService:
    return request.app.state.group_confirmation_service


@router.post(
    "/confirm-group",
    response_model=AlbumProductGroupConfirmationResponse,
)
async def confirm_album_product_group(
    opc_id: str,
    payload: AlbumProductGroupConfirmationRequest,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: AlbumProductGroupConfirmationService = Depends(  # noqa: B008
        get_group_confirmation_service
    ),
) -> AlbumProductGroupConfirmationResponse:
    try:
        result = await service.confirm(
            opc_id=opc_id,
            user_id=membership.user_id,
            confirmation_request_id=payload.confirmation_request_id,
            evidence_asset_ids=payload.evidence_asset_ids,
            source_type=payload.source_type,
            level_1_candidate=payload.level_1_candidate,
            level_2_candidate=payload.level_2_candidate,
        )
    except EvidenceGroupNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from None
    except EvidenceGroupConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    return AlbumProductGroupConfirmationResponse(
        schema_version="opc-album-product-group-confirmation/1.0",
        confirmation_event_id=result.confirmation_event_id,
        album_product_id=result.album_product_id,
        snapshot_version=1,
        opc_id=result.opc_id,
        evidence_asset_ids=result.evidence_asset_ids,
        source_type=result.source_type,
        confirmed_by=result.confirmed_by,
        confirmed_at=result.confirmed_at,
        claims_not_evaluated=(
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        ),
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )


@router.post(
    "/{album_product_id}/confirm-next-snapshot",
    response_model=ExistingProductNextSnapshotResponse,
)
async def confirm_existing_product_next_snapshot(
    opc_id: str,
    album_product_id: str,
    payload: ExistingProductNextSnapshotRequest,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: AlbumProductGroupConfirmationService = Depends(  # noqa: B008
        get_group_confirmation_service
    ),
) -> ExistingProductNextSnapshotResponse:
    try:
        result = await service.confirm_next_snapshot(
            opc_id=opc_id,
            user_id=membership.user_id,
            album_product_id=album_product_id,
            confirmation_request_id=payload.confirmation_request_id,
            expected_current_snapshot_version=(
                payload.expected_current_snapshot_version
            ),
            evidence_asset_ids=payload.evidence_asset_ids,
            source_type=payload.source_type,
            level_1_candidate=payload.level_1_candidate,
            level_2_candidate=payload.level_2_candidate,
        )
    except OpcOwnerRequired as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
        ) from None
    except ExistingProductNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from None
    except (SnapshotVersionConflict, ExistingProductConfirmationConflict) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    return ExistingProductNextSnapshotResponse(
        schema_version="opc-existing-product-next-snapshot/1.0",
        confirmation_event_id=result.confirmation_event_id,
        album_product_id=result.album_product_id,
        snapshot_version=result.snapshot_version,
        opc_id=result.opc_id,
        evidence_asset_ids=result.evidence_asset_ids,
        source_type=result.source_type,
        confirmed_by=result.confirmed_by,
        confirmed_at=result.confirmed_at,
        claims_not_evaluated=(
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        ),
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )
