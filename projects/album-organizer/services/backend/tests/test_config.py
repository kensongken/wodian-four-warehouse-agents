import pytest
from pydantic import ValidationError

from photo_organizer.config import Settings

DEVELOPMENT_PLACEHOLDER_SECRETS = {
    "s3_access_key_id": "minioadmin",
    "s3_secret_access_key": "minioadmin",
    "jwt_signing_key": "local-development-signing-key",
    "otp_pepper": "local-development-otp-pepper",
}


def test_missing_app_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(ValidationError, match="app_env"):
        Settings(_env_file=None)


def test_explicit_development_allows_local_placeholder_secrets() -> None:
    settings = Settings(app_env="development", **DEVELOPMENT_PLACEHOLDER_SECRETS)
    assert settings.app_env == "development"


@pytest.mark.parametrize("app_env", ["test", "production"])
def test_non_development_rejects_local_placeholder_secrets(app_env: str) -> None:
    with pytest.raises(ValidationError, match="development placeholders"):
        Settings(app_env=app_env, **DEVELOPMENT_PLACEHOLDER_SECRETS)
