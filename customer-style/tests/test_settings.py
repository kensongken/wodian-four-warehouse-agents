from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from opc_style.settings import ForegroundPipelineSettings


def _complete_settings(tmp_path: Path) -> dict[str, object]:
    return {
        "enabled": True,
        "executable_path": tmp_path / "runner",
        "model_root": tmp_path / "pipeline",
        "manifest_path": tmp_path / "pipeline/pipeline-manifest.json",
        "pipeline_manifest_sha256": "a" * 64,
    }


def test_pipeline_settings_require_all_absolute_paths_and_digest(tmp_path: Path) -> None:
    assert ForegroundPipelineSettings(_env_file=None).enabled is False
    values = _complete_settings(tmp_path)
    assert ForegroundPipelineSettings(_env_file=None, **values).enabled is True

    for field in ("executable_path", "model_root", "manifest_path"):
        invalid = {**values, field: Path("relative")}
        with pytest.raises(ValidationError, match="foreground_pipeline_unavailable"):
            ForegroundPipelineSettings(_env_file=None, **invalid)
    for digest in (None, "a" * 63, "z" * 64):
        with pytest.raises(ValidationError, match="foreground_pipeline_unavailable"):
            ForegroundPipelineSettings(
                _env_file=None,
                **{**values, "pipeline_manifest_sha256": digest},
            )


def test_pipeline_settings_allow_slow_cold_start_with_a_bounded_timeout(
    tmp_path: Path,
) -> None:
    values = _complete_settings(tmp_path)

    assert (
        ForegroundPipelineSettings(
            _env_file=None,
            **values,
            timeout_seconds=180,
        ).timeout_seconds
        == 180
    )
    with pytest.raises(ValidationError):
        ForegroundPipelineSettings(
            _env_file=None,
            **values,
            timeout_seconds=241,
        )
