from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from photo_organizer.api.dependencies import get_auth_service
from photo_organizer.auth.token_service import InvalidRefreshTokenError
from photo_organizer.schemas.auth import (
    RefreshRequest,
    RequestCodeRequest,
    TokenResponse,
    VerifyCodeRequest,
)
from photo_organizer.services.auth_service import (
    AuthService,
    InvalidInviteError,
    InvalidOtpError,
    OtpDeliveryError,
    RateLimitedError,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/request-code", status_code=status.HTTP_202_ACCEPTED)
async def request_code(
    body: RequestCodeRequest,
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> Response:
    try:
        await auth_service.request_code(
            body.phone_e164,
            body.invite_code,
            request.client.host if request.client is not None else "unknown",
        )
    except InvalidInviteError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid_invite"
        ) from error
    except RateLimitedError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except OtpDeliveryError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="otp_delivery_unavailable",
        ) from error
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/verify-code", response_model=TokenResponse)
async def verify_code(
    body: VerifyCodeRequest,
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> TokenResponse:
    try:
        pair = await auth_service.verify_code(
            body.phone_e164, body.code, body.device_id
        )
    except InvalidOtpError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_otp"
        ) from error
    return TokenResponse.from_pair(pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    auth_service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> TokenResponse:
    try:
        pair = await auth_service.refresh(body.refresh_token, body.device_id)
    except InvalidRefreshTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_refresh_token"
        ) from error
    return TokenResponse.from_pair(pair)
