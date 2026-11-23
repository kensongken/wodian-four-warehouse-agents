from collections import Counter
from math import log2
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


class Settings(BaseSettings):
    """Runtime configuration for the backend and its local dependencies."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_env: Literal["development", "test", "production"]
    database_url: str = (
        "postgresql://photo_organizer:photo_organizer@localhost:5432/photo_organizer"
    )
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "photo-organizer-dev"
    s3_access_key_id: str = "minioadmin"
    s3_secret_access_key: str = "minioadmin"
    jwt_signing_key: SecretStr = SecretStr("local-development-signing-key")
    otp_pepper: SecretStr = SecretStr("local-development-otp-pepper")
    otp_sender: Literal["console", "tencent"] = "console"
    tencent_secret_id: str | None = None
    tencent_secret_key: str | None = None
    tencent_sms_sdk_app_id: str | None = None
    tencent_sms_sign_name: str | None = None
    tencent_sms_template_id: str | None = None
    cors_origins: list[str] = ["http://localhost:3000"]

    @model_validator(mode="after")
    def reject_development_placeholders_outside_development(self) -> Self:
        placeholders = {
            "minioadmin",
            "local-development-signing-key",
            "local-development-otp-pepper",
        }
        secrets = (
            self.s3_access_key_id,
            self.s3_secret_access_key,
            self.jwt_signing_key.get_secret_value(),
            self.otp_pepper.get_secret_value(),
        )
        if self.app_env != "development" and any(
            value in placeholders for value in secrets
        ):
            msg = "development placeholders are not permitted outside development"
            raise ValueError(msg)
        if self.app_env != "development" and self.otp_sender == "console":
            msg = "the console OTP sender is permitted only in development"
            raise ValueError(msg)
        if self.app_env == "production" and not all(
            _is_secure_authentication_secret(secret.get_secret_value())
            for secret in (self.jwt_signing_key, self.otp_pepper)
        ):
            msg = "secure authentication secrets are required in production"
            raise ValueError(msg)
        return self


def _is_secure_authentication_secret(value: str) -> bool:
    encoded = value.encode()
    if len(encoded) < 32:
        return False
    frequencies = Counter(value)
    entropy = -sum(
        (count / len(value)) * log2(count / len(value))
        for count in frequencies.values()
    )
    return entropy >= 3.5
