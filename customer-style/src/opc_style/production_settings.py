"""Explicit production authority; secrets are never returned by probes."""

from decimal import Decimal
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProductionSettings(BaseSettings):
    source_sha: str = Field(default="development", validation_alias="SOURCE_SHA")
    asset_backend: Literal["disabled", "s3"] = Field(
        default="disabled", validation_alias="STYLE_ASSET_BACKEND"
    )
    s3_endpoint: str = Field(default="", validation_alias="STYLE_S3_ENDPOINT")
    s3_region: str = Field(default="", validation_alias="STYLE_S3_REGION")
    s3_bucket: str = Field(default="", validation_alias="STYLE_S3_BUCKET")
    s3_prefix: str = Field(default="style-v1", validation_alias="STYLE_S3_PREFIX")
    s3_auth_mode: Literal["static", "tke_oidc"] = Field(
        default="static", validation_alias="STYLE_S3_AUTH_MODE"
    )
    s3_access_key: SecretStr | None = Field(
        default=None, validation_alias="STYLE_S3_ACCESS_KEY"
    )
    s3_secret_key: SecretStr | None = Field(
        default=None, validation_alias="STYLE_S3_SECRET_KEY"
    )
    s3_session_token: SecretStr | None = Field(
        default=None, validation_alias="STYLE_S3_SESSION_TOKEN"
    )
    tke_region: str = Field(default="", validation_alias="TKE_REGION")
    tke_provider_id: str = Field(default="", validation_alias="TKE_PROVIDER_ID")
    tke_role_arn: str = Field(default="", validation_alias="TKE_ROLE_ARN")
    tke_web_identity_token_file: str = Field(
        default="", validation_alias="TKE_WEB_IDENTITY_TOKEN_FILE"
    )
    storage_capability_secret: SecretStr | None = Field(
        default=None, validation_alias="STYLE_STORAGE_CAPABILITY_SECRET"
    )
    executor_enabled: bool = Field(
        default=False, validation_alias="STYLE_EXECUTOR_ENABLED"
    )
    provider: str = Field(
        default="disabled", validation_alias="STYLE_EXECUTOR_PROVIDER"
    )
    model: str = Field(default="disabled", validation_alias="STYLE_EXECUTOR_MODEL")
    candidate_count: int = Field(default=3, ge=1, le=3)
    maximum_cost_usd: Decimal = Field(
        default=Decimal(0), ge=0, validation_alias="STYLE_EXECUTOR_MAXIMUM_COST_USD"
    )
    estimated_cost_usd: Decimal = Field(
        default=Decimal(0), ge=0, validation_alias="STYLE_EXECUTOR_ESTIMATED_COST_USD"
    )
    lease_seconds: int = Field(default=600, ge=360, le=3600)
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True
    )

    @model_validator(mode="after")
    def storage_authority(self) -> Self:
        if self.asset_backend == "s3":
            endpoint = urlparse(self.s3_endpoint)
            if endpoint.scheme != "https" or not endpoint.netloc or endpoint.query:
                raise ValueError("style_s3_https_endpoint_required")
            if not self.s3_bucket or not self.s3_region:
                raise ValueError("style_s3_scope_required")
            if not self.storage_capability_secret:
                raise ValueError("style_storage_capability_secret_required")
            if self.s3_auth_mode == "static":
                if not all((self.s3_access_key, self.s3_secret_key)):
                    raise ValueError("style_s3_credentials_required")
            else:
                if self.s3_access_key or self.s3_secret_key or self.s3_session_token:
                    raise ValueError("style_s3_oidc_rejects_static_credentials")
                if not all(
                    (
                        self.tke_region,
                        self.tke_provider_id,
                        self.tke_role_arn,
                        self.tke_web_identity_token_file,
                    )
                ):
                    raise ValueError("style_s3_tke_oidc_environment_required")
            if not self.s3_prefix or any(
                part in {"", ".", ".."} for part in self.s3_prefix.split("/")
            ):
                raise ValueError("style_s3_prefix_invalid")
        if self.executor_enabled:
            if self.provider != "openai" or self.model != "gpt-image-2":
                raise ValueError("style_executor_provider_not_supported")
            if self.candidate_count != 3:
                raise ValueError("style_executor_requires_three_candidates")
            if not 0 < self.estimated_cost_usd <= self.maximum_cost_usd:
                raise ValueError("style_executor_budget_required")
        return self
