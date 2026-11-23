from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from photo_organizer.auth.token_service import TokenService
from photo_organizer.models.opc import OpcMembership
from photo_organizer.models.user import User
from photo_organizer.services.auth_service import AuthService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    async with session_factory() as session:
        yield session


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_bearer_token"
        )
    token_service: TokenService = request.app.state.token_service
    try:
        claims = token_service.decode_access_token(credentials.credentials)
        user_id = UUID(str(claims["sub"]))
    except (ValueError, KeyError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_bearer_token"
        ) from error
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_bearer_token"
        )
    return user


async def require_opc_membership(
    opc_id: Annotated[
        str,
        Path(
            min_length=3,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
        ),
    ],
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> OpcMembership:
    membership = await session.scalar(
        select(OpcMembership).where(
            OpcMembership.opc_id == opc_id,
            OpcMembership.user_id == user.id,
        )
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="opc_not_found",
        )
    return membership
