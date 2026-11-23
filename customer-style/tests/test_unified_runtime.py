"""The local page must distinguish the running candidate from old evidence."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opc_style.main import create_app


def test_local_runtime_reports_actual_engine_and_keeps_secrets_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.sqlite3'}")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(tmp_path / "assets"))
    monkeypatch.setenv("REAL_IMAGE_ENABLED", "false")
    monkeypatch.setenv("REAL_IMAGE_API_KEY", "private-test-key-never-render")
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/local/style-suite/api/acceptance-runtime")
        assert response.status_code == 200
        runtime = response.json()
        assert runtime["foreground_engine"] == "disabled"
        assert runtime["paid_generation_enabled"] is False
        assert runtime["zero_paid_mode"] is True
        assert runtime["generation_backend"] == {
            "adapter_version": "deterministic-local-image-v1",
            "generation_mode": "synthetic_test_only",
            "visual_quality_acceptance_allowed": False,
            "workflow_acceptance_allowed": True,
        }
        assert runtime["paid_provider_states"] == {
            "image_generation": False,
            "background_generation": False,
            "style_analysis": False,
            "product_identity": False,
            "fidelity_assessment": False,
            "missing_part_completion": False,
        }
        assert runtime["publishable"] is False
        assert len(runtime["revision"]) == 40
        assert "private-test-key-never-render" not in response.text
        assert str(tmp_path) not in response.text
        app.state.product_foreground_extractor = object()
        assert client.get("/local/style-suite/api/acceptance-runtime").json()[
            "foreground_engine"
        ] == "unrecognized"
