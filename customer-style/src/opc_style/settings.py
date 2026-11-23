"""Settings for the removable classification-to-style local test bridge."""

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES = 10 * 1024 * 1024
LOCAL_PRODUCT_UPLOAD_HARD_MAX_DIMENSION = 4096
LOCAL_PRODUCT_UPLOAD_HARD_MAX_PIXELS = 16_000_000


class StyleSuitePlanningSettings(BaseSettings):
    """Startup-owned planning assumptions, never a provider billing quote."""

    candidate_estimate_usd: Decimal = Field(
        default=Decimal("0.12"),
        ge=0,
        max_digits=12,
        decimal_places=6,
        validation_alias="STYLE_SUITE_CANDIDATE_ESTIMATE_USD",
    )
    real_generation_enabled: bool = Field(
        default=False, validation_alias="REAL_IMAGE_ENABLED"
    )
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, frozen=True
    )

    @model_validator(mode="after")
    def require_positive_real_estimate(self) -> Self:
        if self.real_generation_enabled and self.candidate_estimate_usd == 0:
            raise ValueError("style_suite_real_generation_estimate_required")
        return self


class StyleWarehouseV3Settings(BaseSettings):
    """Fail-closed configuration for the independent style intake API."""

    enabled: bool = Field(
        default=False, validation_alias="STYLE_WAREHOUSE_V3_ENABLED"
    )
    signing_secret: SecretStr | None = Field(
        default=None, validation_alias="STYLE_WAREHOUSE_SIGNING_SECRET"
    )
    signature_tolerance_seconds: int = Field(
        default=300,
        ge=1,
        le=900,
        validation_alias="STYLE_WAREHOUSE_SIGNATURE_TOLERANCE_SECONDS",
    )
    classification_media_base_url: str | None = Field(
        default=None, validation_alias="CLASSIFICATION_MEDIA_BASE_URL"
    )
    classification_media_secret: SecretStr | None = Field(
        default=None, validation_alias="CLASSIFICATION_STYLE_MEDIA_SECRET"
    )
    verify_source_images: bool = Field(
        default=True, validation_alias="STYLE_WAREHOUSE_VERIFY_SOURCE_IMAGES"
    )
    media_max_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1,
        le=20 * 1024 * 1024,
        validation_alias="STYLE_WAREHOUSE_MEDIA_MAX_BYTES",
    )
    mall_product_asset_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
        validation_alias="MALL_PRODUCT_ASSET_MAX_BYTES",
    )
    mall_product_asset_max_width: int = Field(
        default=4096,
        gt=0,
        le=16384,
        validation_alias="MALL_PRODUCT_ASSET_MAX_WIDTH",
    )
    mall_product_asset_max_height: int = Field(
        default=4096,
        gt=0,
        le=16384,
        validation_alias="MALL_PRODUCT_ASSET_MAX_HEIGHT",
    )
    mall_product_asset_max_pixels: int = Field(
        default=16 * 1024 * 1024,
        gt=0,
        le=64 * 1024 * 1024,
        validation_alias="MALL_PRODUCT_ASSET_MAX_PIXELS",
    )
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, frozen=True
    )

    @model_validator(mode="after")
    def require_signing_secret_when_enabled(self) -> Self:
        if self.enabled and not (
            self.signing_secret and self.signing_secret.get_secret_value().strip()
        ):
            raise ValueError("style_warehouse_signing_secret_required")
        if (
            self.enabled
            and self.verify_source_images
            and not (
                self.classification_media_base_url
                and self.classification_media_secret
                and self.classification_media_secret.get_secret_value().strip()
            )
        ):
            raise ValueError("style_warehouse_classification_media_config_required")
        if self.classification_media_base_url:
            parsed = urlsplit(self.classification_media_base_url)
            if parsed.scheme != "https" and not (
                parsed.scheme == "http"
                and parsed.hostname
                in {"127.0.0.1", "localhost", "classification-api"}
            ):
                raise ValueError("style_warehouse_classification_media_url_invalid")
        return self


class RealImageProviderSettings(BaseSettings):
    """Explicit authority and hard limits for the optional paid image provider."""

    enabled: bool = Field(default=False, validation_alias="REAL_IMAGE_ENABLED")
    api_key: SecretStr | None = Field(
        default=None, validation_alias="REAL_IMAGE_API_KEY"
    )
    model: Literal["gpt-image-2"] = Field(
        default="gpt-image-2", validation_alias="REAL_IMAGE_MODEL"
    )
    max_run_usd: Decimal = Field(
        default=Decimal(0),
        ge=0,
        validation_alias="REAL_IMAGE_MAX_RUN_USD",
    )
    estimated_request_usd: Decimal = Field(
        default=Decimal("0.30"),
        gt=0,
        validation_alias="REAL_IMAGE_ESTIMATED_REQUEST_USD",
    )
    candidate_count: Literal[1, 3] = Field(
        default=3, validation_alias="REAL_IMAGE_CANDIDATE_COUNT"
    )
    # A controlled paid pilot has one durable pre-dispatch reservation; retrying
    # automatically could turn one confirmed authorization into multiple charges.
    max_retries_per_candidate: Literal[0] = 0
    timeout_seconds: int = Field(default=300, ge=10, le=300)

    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True
    )

    def __init__(self, **data: Any) -> None:
        """Accept paid authority only from explicit values or process environment."""
        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @field_validator("candidate_count", mode="before")
    @classmethod
    def parse_candidate_count(cls, value: object) -> object:
        """Parse the process environment while retaining the literal hard bound."""
        return int(value) if isinstance(value, str) and value.isdigit() else value

    @model_validator(mode="after")
    def require_real_provider_authority(self) -> Self:
        """Reject paid-provider startup unless both authority gates are present."""

        if self.enabled and not (self.api_key and self.api_key.get_secret_value()):
            raise ValueError("real_image_api_key_required")
        if self.enabled and self.max_run_usd <= 0:
            raise ValueError("real_image_budget_required")
        return self


class RealBackgroundProviderSettings(BaseSettings):
    """Explicit one-attempt authority for empty-background generation."""

    enabled: bool = False
    api_key: SecretStr | None = None
    model: Literal["gpt-image-2"] = "gpt-image-2"
    max_run_usd: Decimal = Field(default=Decimal(0), ge=0)
    timeout_seconds: int = Field(default=300, ge=10, le=300)
    model_config = SettingsConfigDict(
        env_prefix="OPC_STYLE_REAL_BACKGROUND_",
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        frozen=True,
    )

    def __init__(self, **data: Any) -> None:
        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @model_validator(mode="after")
    def require_authority(self) -> Self:
        if self.enabled and not (
            self.api_key and self.api_key.get_secret_value().strip()
        ):
            raise ValueError("real_background_api_key_required")
        if self.enabled and self.max_run_usd <= 0:
            raise ValueError("real_background_budget_required")
        return self


class MissingPartCompletionSettings(BaseSettings):
    """Explicit one-attempt authority for AI-simulated jewelry repair."""

    enabled: bool = Field(
        default=False, validation_alias="MISSING_PART_COMPLETION_ENABLED"
    )
    api_key: SecretStr | None = Field(
        default=None, validation_alias="MISSING_PART_COMPLETION_API_KEY"
    )
    model: Literal["gpt-image-2.5-sunburst-2026-09-08"] = Field(
        default="gpt-image-2.5-sunburst-2026-09-08",
        validation_alias="MISSING_PART_COMPLETION_MODEL",
    )
    max_run_usd: Decimal = Field(
        default=Decimal(0),
        ge=0,
        validation_alias="MISSING_PART_COMPLETION_MAX_RUN_USD",
    )
    timeout_seconds: int = Field(
        default=300,
        ge=10,
        le=300,
        validation_alias="MISSING_PART_COMPLETION_TIMEOUT_SECONDS",
    )
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True, frozen=True
    )

    def __init__(self, **data: Any) -> None:
        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @model_validator(mode="after")
    def require_authority(self) -> Self:
        if self.enabled and not (
            self.api_key and self.api_key.get_secret_value().strip()
        ):
            raise ValueError("missing_part_api_key_required")
        if self.enabled and self.max_run_usd <= 0:
            raise ValueError("missing_part_budget_required")
        return self


class ForegroundExtractorSettings(BaseSettings):
    """Opt-in limits for the explicitly installed local foreground executable."""

    enabled: bool = Field(
        default=False, validation_alias="FOREGROUND_EXTRACTOR_ENABLED"
    )
    binary_path: Path | None = Field(
        default=None, validation_alias="FOREGROUND_EXTRACTOR_BINARY_PATH"
    )
    extractor_version: str = Field(
        default="macos-vision-foreground-v1",
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$",
        validation_alias="FOREGROUND_EXTRACTOR_VERSION",
    )
    timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        validation_alias="FOREGROUND_EXTRACTOR_TIMEOUT_SECONDS",
    )
    max_output_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
        validation_alias="FOREGROUND_EXTRACTOR_MAX_OUTPUT_BYTES",
    )
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True, frozen=True
    )

    def __init__(self, **data: Any) -> None:
        """Read only explicit values or process environment, never a dotenv file."""
        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @model_validator(mode="after")
    def require_explicit_absolute_binary(self) -> Self:
        if self.enabled and (
            self.binary_path is None or not self.binary_path.is_absolute()
        ):
            raise ValueError("foreground_extractor_unavailable")
        return self


class ForegroundBenchmarkSettings(BaseSettings):
    """Disabled-by-default access to one repository-external private gold set."""

    enabled: bool = Field(
        default=False, validation_alias="FOREGROUND_BENCHMARK_ENABLED"
    )
    root: Path | None = Field(
        default=None, validation_alias="FOREGROUND_BENCHMARK_ROOT"
    )
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True, frozen=True
    )

    def __init__(self, **data: Any) -> None:
        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @model_validator(mode="after")
    def require_explicit_absolute_root(self) -> Self:
        if self.enabled and (self.root is None or not self.root.is_absolute()):
            raise ValueError("foreground_benchmark_unavailable")
        return self


class ForegroundBenchmarkWorkbenchSettings(BaseSettings):
    """Disabled-by-default repository-external benchmark drafting root."""

    enabled: bool = Field(
        default=False,
        validation_alias="FOREGROUND_BENCHMARK_WORKBENCH_ENABLED",
    )
    root: Path | None = Field(
        default=None,
        validation_alias="FOREGROUND_BENCHMARK_WORKBENCH_ROOT",
    )
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True, frozen=True
    )

    def __init__(self, **data: Any) -> None:
        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @model_validator(mode="after")
    def require_explicit_absolute_root(self) -> Self:
        if self.enabled and (self.root is None or not self.root.is_absolute()):
            raise ValueError("foreground_benchmark_workbench_unavailable")
        return self


class ForegroundPipelineSettings(BaseSettings):
    """Fail-closed configuration for the sealed local dual-model pipeline."""

    enabled: bool = Field(
        default=False, validation_alias="FOREGROUND_PIPELINE_ENABLED"
    )
    executable_path: Path | None = Field(
        default=None, validation_alias="FOREGROUND_PIPELINE_EXECUTABLE_PATH"
    )
    model_root: Path | None = Field(
        default=None, validation_alias="FOREGROUND_PIPELINE_MODEL_ROOT"
    )
    manifest_path: Path | None = Field(
        default=None, validation_alias="FOREGROUND_PIPELINE_MANIFEST_PATH"
    )
    pipeline_manifest_sha256: str | None = Field(
        default=None,
        validation_alias="FOREGROUND_PIPELINE_MANIFEST_SHA256",
    )
    timeout_seconds: int = Field(
        default=20,
        ge=1,
        le=240,
        validation_alias="FOREGROUND_PIPELINE_TIMEOUT_SECONDS",
    )
    max_output_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1024,
        validation_alias="FOREGROUND_PIPELINE_MAX_OUTPUT_BYTES",
    )
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True, frozen=True
    )

    def __init__(self, **data: Any) -> None:
        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @model_validator(mode="after")
    def require_explicit_absolute_installation(self) -> Self:
        paths = (self.executable_path, self.model_root, self.manifest_path)
        if self.enabled and (
            any(path is None or not path.is_absolute() for path in paths)
            or self.pipeline_manifest_sha256 is None
            or len(self.pipeline_manifest_sha256) != 64
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in self.pipeline_manifest_sha256
            )
        ):
            raise ValueError("foreground_pipeline_unavailable")
        return self


# Historical import compatibility only. New startup code uses the explicit name above.
LocalForegroundWorkerSettings = ForegroundPipelineSettings


class RealStyleAnalysisSettings(BaseSettings):
    """Explicit opt-in and process-local spending ceiling for visual proposals."""

    enabled: bool = Field(default=False, validation_alias="REAL_STYLE_ANALYSIS_ENABLED")
    api_key: SecretStr | None = Field(
        default=None, validation_alias="REAL_STYLE_ANALYSIS_API_KEY"
    )
    model: str = Field(
        default="gpt-4.1-mini-2025-04-14", validation_alias="REAL_STYLE_ANALYSIS_MODEL"
    )
    max_run_usd: Decimal = Field(
        default=Decimal(0), ge=0, validation_alias="REAL_STYLE_ANALYSIS_MAX_RUN_USD"
    )
    timeout_seconds: int = Field(
        default=60,
        ge=1,
        le=120,
        validation_alias="REAL_STYLE_ANALYSIS_TIMEOUT_SECONDS",
    )
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )

    @model_validator(mode="after")
    def require_authority(self) -> Self:
        if self.enabled and not (
            self.api_key and self.api_key.get_secret_value().strip()
        ):
            raise ValueError("real_style_analysis_api_key_required")
        if self.enabled and self.max_run_usd <= 0:
            raise ValueError("real_style_analysis_budget_required")
        return self


class RealFidelitySettings(BaseSettings):
    """Explicit opt-in and spending ceiling for visual identity assessment."""

    enabled: bool = Field(default=False, validation_alias="REAL_FIDELITY_ENABLED")
    api_key: SecretStr | None = Field(
        default=None, validation_alias="REAL_FIDELITY_API_KEY"
    )
    model: Literal["gpt-4.1-mini-2025-04-14"] = Field(
        default="gpt-4.1-mini-2025-04-14",
        validation_alias="REAL_FIDELITY_MODEL",
    )
    max_run_usd: Decimal = Field(
        default=Decimal(0), ge=0, validation_alias="REAL_FIDELITY_MAX_RUN_USD"
    )
    timeout_seconds: int = Field(default=60, ge=1, le=120)
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True
    )

    def __init__(self, **data: Any) -> None:
        """Accept external authority only from explicit values or process environment."""
        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @model_validator(mode="after")
    def require_authority(self) -> Self:
        if self.enabled and not (
            self.api_key and self.api_key.get_secret_value().strip()
        ):
            raise ValueError("real_fidelity_api_key_required")
        if self.enabled and self.max_run_usd <= 0:
            raise ValueError("real_fidelity_budget_required")
        return self


class RealProductIdentitySettings(BaseSettings):
    """Explicit opt-in and spending authority for identity-card proposals."""

    enabled: bool = Field(
        default=False, validation_alias="REAL_PRODUCT_IDENTITY_ENABLED"
    )
    api_key: SecretStr | None = Field(
        default=None, validation_alias="REAL_PRODUCT_IDENTITY_API_KEY"
    )
    model: str = Field(
        default="gpt-4.1-mini-2025-04-14",
        validation_alias="REAL_PRODUCT_IDENTITY_MODEL",
    )
    max_run_usd: Decimal = Field(
        default=Decimal(0),
        ge=0,
        validation_alias="REAL_PRODUCT_IDENTITY_MAX_RUN_USD",
    )
    timeout_seconds: int = Field(
        default=60,
        ge=1,
        le=120,
        validation_alias="REAL_PRODUCT_IDENTITY_TIMEOUT_SECONDS",
    )
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", populate_by_name=True
    )

    def __init__(self, **data: Any) -> None:
        """Accept paid authority only from explicit values or process environment."""

        data.pop("_env_file", None)
        super().__init__(_env_file=None, **data)

    @model_validator(mode="after")
    def require_authority(self) -> Self:
        if self.enabled and not (
            self.api_key and self.api_key.get_secret_value().strip()
        ):
            raise ValueError("real_product_identity_api_key_required")
        if self.enabled and self.max_run_usd <= 0:
            raise ValueError("real_product_identity_budget_required")
        return self


class LocalBridgeSettings(BaseSettings):
    """Local-only limits for consumer-owned temporary media."""

    local_classification_bridge: bool = False
    local_classification_bridge_nonce: SecretStr | None = None
    local_classification_bridge_stage_root: Path = Path(
        ".local-classification-bridge-media"
    )
    local_classification_bridge_asset_root: Path | None = None
    local_style_asset_root: Path | None = Field(
        default=None, validation_alias="LOCAL_STYLE_ASSET_ROOT"
    )
    local_classification_bridge_authenticity_failure_product_id: str | None = Field(
        default=None,
        pattern=r"^test-product-[a-z0-9]+(?:-[a-z0-9]+)*$",
        max_length=128,
    )
    local_classification_bridge_max_bytes: int = Field(
        default=LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES,
        gt=0,
        le=LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES,
    )
    local_classification_bridge_receipt_ttl_seconds: int = Field(
        default=300, gt=0, le=3600
    )
    local_classification_bridge_max_width: int = Field(
        default=LOCAL_PRODUCT_UPLOAD_HARD_MAX_DIMENSION,
        gt=0,
        le=LOCAL_PRODUCT_UPLOAD_HARD_MAX_DIMENSION,
    )
    local_classification_bridge_max_height: int = Field(
        default=LOCAL_PRODUCT_UPLOAD_HARD_MAX_DIMENSION,
        gt=0,
        le=LOCAL_PRODUCT_UPLOAD_HARD_MAX_DIMENSION,
    )
    local_classification_bridge_max_pixels: int = Field(
        default=LOCAL_PRODUCT_UPLOAD_HARD_MAX_PIXELS,
        gt=0,
        le=LOCAL_PRODUCT_UPLOAD_HARD_MAX_PIXELS,
    )
    local_classification_bridge_max_frames: int = Field(default=1, gt=0, le=8)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("local_style_asset_root")
    @classmethod
    def require_absolute_local_style_asset_root(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("local_style_asset_root_absolute")
        return value

    @model_validator(mode="after")
    def require_startup_nonce_when_bridge_is_enabled(self) -> Self:
        """Fail closed instead of exposing an enabled bridge without a nonce."""

        nonce = self.local_classification_bridge_nonce
        if self.local_classification_bridge and (
            nonce is None or not nonce.get_secret_value()
        ):
            raise ValueError("local_classification_bridge_nonce_required")
        if self.local_classification_bridge:
            stage_root = self.local_classification_bridge_stage_root
            asset_root = self.local_classification_bridge_asset_root
            if asset_root is None:
                raise ValueError("local_classification_bridge_asset_root_required")
            roots = (stage_root, asset_root)
            if any(not root.is_absolute() for root in roots):
                raise ValueError("local_classification_bridge_absolute_roots_required")
            normalized = tuple(root.resolve(strict=False) for root in roots)
            if normalized != roots:
                raise ValueError("local_classification_bridge_canonical_roots_required")
            if (
                normalized[0] == normalized[1]
                or normalized[0] in normalized[1].parents
                or normalized[1] in normalized[0].parents
            ):
                raise ValueError("local_classification_bridge_roots_overlap")
        return self


def resolve_local_style_asset_root(
    settings: LocalBridgeSettings, project_root: Path
) -> Path:
    """Resolve the local-only private root without exposing it through the API."""
    return (
        settings.local_style_asset_root or project_root / ".local-style-assets"
    ).resolve()
