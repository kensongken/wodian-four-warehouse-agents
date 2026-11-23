from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str
    database_url: str
    s3_endpoint_url: AnyHttpUrl
    s3_region: str
    s3_bucket: str
    s3_access_key_id: SecretStr
    s3_secret_access_key: SecretStr
    album_signing_secret: SecretStr
    sales_signing_secret: SecretStr
    style_signing_secret: SecretStr
    admin_signing_secret: SecretStr
    sales_webhook_url: AnyHttpUrl
    signature_tolerance_seconds: int = Field(default=300, ge=1, le=3600)
