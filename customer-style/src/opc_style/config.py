"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the local style agent service."""

    app_env: str = "development"
    database_url: str = "sqlite:///./opc_style.db"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
