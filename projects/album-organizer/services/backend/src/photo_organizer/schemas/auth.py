from pydantic import BaseModel, Field

from photo_organizer.auth.token_service import TokenPair


class RequestCodeRequest(BaseModel):
    phone_e164: str = Field(pattern=r"^\+[1-9]\d{1,14}$", max_length=16)
    invite_code: str = Field(min_length=1, max_length=128)


class VerifyCodeRequest(BaseModel):
    phone_e164: str = Field(pattern=r"^\+[1-9]\d{1,14}$", max_length=16)
    code: str = Field(pattern=r"^\d{6}$")
    device_id: str = Field(min_length=8, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)
    device_id: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in_seconds: int

    @classmethod
    def from_pair(cls, pair: TokenPair) -> "TokenResponse":
        return cls(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in_seconds=pair.expires_in_seconds,
        )
