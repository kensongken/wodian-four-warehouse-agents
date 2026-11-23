from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REAL_PROVIDER_FLAGS = (
    "REAL_IMAGE_ENABLED",
    "REAL_STYLE_ANALYSIS_ENABLED",
    "REAL_FIDELITY_ENABLED",
    "REAL_PRODUCT_IDENTITY_ENABLED",
)


@dataclass(frozen=True)
class PrivateStagingEnvironment:
    database_path: Path
    signing_secret: bytes
    media_secret: bytes


def _database_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("private_staging_database_must_be_under_data")
    raw_path = database_url.removeprefix(prefix)
    path = Path(raw_path)
    resolved = path.resolve(strict=False)
    data_root = Path("/data")
    if (
        not path.is_absolute()
        or resolved == data_root
        or data_root not in resolved.parents
    ):
        raise ValueError("private_staging_database_must_be_under_data")
    return resolved


def _private_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "classification-api",
    }


def validate_private_staging_environment(
    env: Mapping[str, str],
) -> PrivateStagingEnvironment:
    if env.get("APP_ENV") != "private_staging":
        raise ValueError("private_staging_app_env_required")
    if env.get("PRIVATE_STAGING_SYNTHETIC_ONLY") != "confirmed":
        raise ValueError("private_staging_synthetic_only_confirmation_required")
    if any(env.get(name) != "false" for name in REAL_PROVIDER_FLAGS):
        raise ValueError("private_staging_real_providers_must_be_false")
    if any(env.get(name) for name in env if name.endswith("_API_KEY")):
        raise ValueError("private_staging_provider_credentials_forbidden")
    if env.get("STYLE_WAREHOUSE_V3_ENABLED") != "true":
        raise ValueError("style_warehouse_v3_must_be_enabled")
    if env.get("STYLE_WAREHOUSE_VERIFY_SOURCE_IMAGES") != "true":
        raise ValueError("style_warehouse_source_image_verification_required")

    signing_secret = env.get("STYLE_WAREHOUSE_SIGNING_SECRET", "")
    media_secret = env.get("CLASSIFICATION_STYLE_MEDIA_SECRET", "")
    if len(signing_secret.encode()) < 32:
        raise ValueError("style_warehouse_signing_secret_too_short")
    if len(media_secret.encode()) < 32 or media_secret == signing_secret:
        raise ValueError("style_warehouse_media_secret_invalid")
    if not _private_http_url(env.get("CLASSIFICATION_MEDIA_BASE_URL", "")):
        raise ValueError("classification_private_media_url_required")

    return PrivateStagingEnvironment(
        database_path=_database_path(env.get("DATABASE_URL", "")),
        signing_secret=signing_secret.encode(),
        media_secret=media_secret.encode(),
    )


def main() -> None:
    validate_private_staging_environment(os.environ)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
    )
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "opc_style.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ],
    )


if __name__ == "__main__":
    main()
