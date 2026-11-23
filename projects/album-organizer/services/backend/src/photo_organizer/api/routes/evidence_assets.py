from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request, status

from photo_organizer.api.dependencies import require_opc_membership
from photo_organizer.catalog.evidence_assets import (
    EvidenceAssetConflict,
    EvidenceAssetNotFound,
    EvidenceAssetRegistrationService,
)
from photo_organizer.models.opc import OpcMembership
from photo_organizer.schemas.evidence_assets import (
    EvidenceAssetRegistrationRequest,
    EvidenceAssetResponse,
    EvidenceMediaType,
)

router = APIRouter(
    prefix="/v1/opcs/{opc_id}/evidence-assets",
    tags=["evidence-assets"],
)


def get_evidence_asset_service(request: Request) -> EvidenceAssetRegistrationService:
    return request.app.state.evidence_asset_service


@router.post("", response_model=EvidenceAssetResponse)
async def register_evidence_asset(
    opc_id: str,
    payload: EvidenceAssetRegistrationRequest,
    membership: OpcMembership = Depends(require_opc_membership),  # noqa: B008
    service: EvidenceAssetRegistrationService = Depends(  # noqa: B008
        get_evidence_asset_service
    ),
) -> EvidenceAssetResponse:
    try:
        evidence = await service.register(
            opc_id=opc_id,
            user_id=membership.user_id,
            source_asset_id=payload.source_asset_id,
        )
    except EvidenceAssetNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from None
    except EvidenceAssetConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    return EvidenceAssetResponse(
        schema_version="opc-evidence-asset/1.0",
        evidence_asset_id=evidence.id,
        opc_id=evidence.opc_id,
        source_asset_id=evidence.source_asset_id,
        storage_ref=evidence.storage_ref,
        sha256=evidence.sha256,
        byte_size=evidence.byte_size,
        media_type=cast(EvidenceMediaType, evidence.media_type),
        captured_at=evidence.captured_at,
        imported_at=evidence.imported_at,
    )
