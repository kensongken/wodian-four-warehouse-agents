from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from photo_organizer.api.dependencies import get_current_user, get_session
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.user import User
from photo_organizer.schemas.opcs import MyOpcListResponse, MyOpcResponse

router = APIRouter(prefix="/v1/me", tags=["me"])


@router.get("/opcs", response_model=MyOpcListResponse)
async def list_my_opcs(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MyOpcListResponse:
    rows = (
        await session.execute(
            select(OpcWorkspace, OpcMembership)
            .join(
                OpcMembership,
                OpcMembership.opc_id == OpcWorkspace.id,
            )
            .where(OpcMembership.user_id == user.id)
            .order_by(OpcWorkspace.display_name, OpcWorkspace.id)
        )
    ).all()
    return MyOpcListResponse(
        items=[
            MyOpcResponse(
                id=workspace.id,
                display_name=workspace.display_name,
                role=membership.role,
            )
            for workspace, membership in rows
        ]
    )
