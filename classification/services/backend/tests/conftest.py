"""Shared test fixtures are added task-by-task as the integration spine grows."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://product_engine:product_engine_dev@localhost:5432/product_engine",
)


@pytest.fixture
def alembic_runner() -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=BACKEND_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    return run


@pytest.fixture
def valid_album_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "11111111-1111-4111-8111-111111111111",
        "occurred_at": "2026-08-15T08:00:00Z",
        "tenant_id": "22222222-2222-4222-8222-222222222222",
        "source_task_id": "33333333-3333-4333-8333-333333333333",
        "product_code": "SP-00831",
        "category": "南红",
        "form": "手串",
        "classification_confidence": 0.97,
        "images": [
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "object_key": (
                    "tenants/22222222-2222-4222-8222-222222222222/"
                    "products/SP-00831/source/main.heic"
                ),
                "sha256": "a" * 64,
                "mime_type": "image/heic",
                "kind": "original",
                "role": "main_candidate",
                "quality_confidence": 0.91,
            }
        ],
    }
