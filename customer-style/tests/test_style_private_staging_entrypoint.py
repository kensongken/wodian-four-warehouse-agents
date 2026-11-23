import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/private_staging_entrypoint.py"
DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile.private-staging"
SPEC = importlib.util.spec_from_file_location(
    "style_private_staging_entrypoint", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
validate_private_staging_environment = MODULE.validate_private_staging_environment


def valid_env(**updates: str) -> dict[str, str]:
    value = {
        "APP_ENV": "private_staging",
        "PRIVATE_STAGING_SYNTHETIC_ONLY": "confirmed",
        "DATABASE_URL": "sqlite:////data/style.sqlite3",
        "STYLE_WAREHOUSE_V3_ENABLED": "true",
        "STYLE_WAREHOUSE_SIGNING_SECRET": "classification-to-style-secret-at-least-32-bytes",
        "STYLE_WAREHOUSE_VERIFY_SOURCE_IMAGES": "true",
        "CLASSIFICATION_MEDIA_BASE_URL": "http://classification-api:8000",
        "CLASSIFICATION_STYLE_MEDIA_SECRET": "style-media-read-secret-at-least-32-bytes",
        "REAL_IMAGE_ENABLED": "false",
        "REAL_STYLE_ANALYSIS_ENABLED": "false",
        "REAL_FIDELITY_ENABLED": "false",
        "REAL_PRODUCT_IDENTITY_ENABLED": "false",
    }
    value.update(updates)
    return value


def test_accepts_private_database_and_authenticated_image_reads() -> None:
    configured = validate_private_staging_environment(valid_env())

    assert configured.database_path == Path("/data/style.sqlite3")
    assert configured.media_secret == b"style-media-read-secret-at-least-32-bytes"


def test_private_staging_container_installs_the_frozen_runtime_lock() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock alembic.ini ./" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "pip install --no-cache-dir ." not in dockerfile


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"DATABASE_URL": "sqlite:////tmp/style.sqlite3"}, "must_be_under_data"),
        ({"CLASSIFICATION_MEDIA_BASE_URL": "https://example.com"}, "private_media_url"),
        (
            {"STYLE_WAREHOUSE_VERIFY_SOURCE_IMAGES": "false"},
            "image_verification_required",
        ),
        ({"REAL_IMAGE_ENABLED": "true"}, "providers_must_be_false"),
        ({"CLASSIFICATION_STYLE_MEDIA_SECRET": "short"}, "media_secret_invalid"),
    ],
)
def test_fails_closed_outside_synthetic_private_media_boundary(
    updates: dict[str, str], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        validate_private_staging_environment(valid_env(**updates))
